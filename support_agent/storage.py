"""Durable, single-process test service state. Never imports the shop database."""
import json
import sqlite3
import time
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
          INSERT OR IGNORE INTO settings VALUES ('mode','off'),('revision','0'),('offset','0');
          CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY,enabled INTEGER,can_reply INTEGER);
          CREATE TABLE IF NOT EXISTS chats(
            id INTEGER PRIMARY KEY, connection TEXT, uid INTEGER, mode TEXT DEFAULT 'draft',
            revision INTEGER DEFAULT 0, latest INTEGER DEFAULT 0, inbound REAL DEFAULT 0,
            UNIQUE(connection,uid));
          CREATE TABLE IF NOT EXISTS messages(
            chat INTEGER, mid INTEGER, role TEXT, text TEXT, media TEXT, at REAL,
            state TEXT, PRIMARY KEY(chat,mid));
          CREATE TABLE IF NOT EXISTS drafts(
            id INTEGER PRIMARY KEY, chat INTEGER, mid INTEGER, revision INTEGER,
            global_revision INTEGER, text TEXT, summary TEXT, handoff INTEGER,
            created REAL, state TEXT DEFAULT 'ready', admin_mid INTEGER,
            telegram_mid INTEGER);
          CREATE TABLE IF NOT EXISTS quota(day TEXT PRIMARY KEY,calls INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS chat_profiles(
            chat INTEGER PRIMARY KEY, username TEXT, name TEXT);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def get(self, key):
        return self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))

    def chat(self, cid):
        return self.db.execute('SELECT * FROM chats WHERE id=?', (cid,)).fetchone()

    def save_profile(self, cid, username, name):
        self.db.execute('INSERT OR REPLACE INTO chat_profiles VALUES (?,?,?)',
                        (cid,username,name))

    def profile(self, cid):
        return self.db.execute('SELECT * FROM chat_profiles WHERE chat=?',(cid,)).fetchone()

    def switch(self, mode, cid=None):
        if mode not in ('off', 'draft'):
            raise ValueError('Automatic sending is not enabled in the pilot')
        with self.transaction():
            if cid is None:
                self.put('mode', mode)
                self.put('revision', int(self.get('revision')) + 1)
                self.db.execute("UPDATE messages SET state='stale' WHERE state IN ('queued','working')")
                self.db.execute("UPDATE drafts SET state='stale' WHERE state='ready'")
            else:
                self.db.execute('UPDATE chats SET mode=?,revision=revision+1 WHERE id=?', (mode,cid))
                self.db.execute("UPDATE messages SET state='stale' WHERE chat=? AND state IN ('queued','working')", (cid,))
                self.db.execute("UPDATE drafts SET state='stale' WHERE chat=? AND state='ready'", (cid,))

    def connection(self, ident, enabled, can_reply):
        with self.transaction():
            previous = self.db.execute('SELECT * FROM connections WHERE id=?', (ident,)).fetchone()
            self.db.execute('INSERT OR REPLACE INTO connections VALUES (?,?,?)',
                            (ident, int(enabled), int(can_reply)))
            if previous and (previous['enabled'] != int(enabled) or previous['can_reply'] != int(can_reply)):
                self.db.execute('UPDATE chats SET revision=revision+1 WHERE connection=?', (ident,))

    def ingest(self, connection, uid, mid, role, text, media, at, edited=False):
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO chats(connection,uid) VALUES (?,?)', (connection,uid))
            chat = self.db.execute('SELECT * FROM chats WHERE connection=? AND uid=?', (connection,uid)).fetchone()
            cid = chat['id']
            existing = self.db.execute('SELECT 1 FROM messages WHERE chat=? AND mid=?', (cid,mid)).fetchone()
            if existing and not edited:
                return cid
            # Any manual owner message or edit invalidates pending suggestions.
            if role == 'owner':
                self.db.execute("UPDATE chats SET mode='off',revision=revision+1 WHERE id=?", (cid,))
            else:
                self.db.execute('UPDATE chats SET latest=MAX(latest,?),inbound=MAX(inbound,?),revision=revision+1 WHERE id=?', (mid,at,cid))
            self.db.execute("UPDATE messages SET state='stale' WHERE chat=? AND state IN ('queued','working')", (cid,))
            self.db.execute("UPDATE drafts SET state='stale' WHERE chat=? AND state='ready'", (cid,))
            chat = self.chat(cid)
            queued = role == 'client' and mid == chat['latest'] and chat['mode']=='draft' and self.get('mode')=='draft'
            self.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)',
                            (cid,mid,role,text,json.dumps(media),at,'queued' if queued else 'stored'))
            return cid

    def delete_messages(self, connection, uid, mids):
        with self.transaction():
            row=self.db.execute('SELECT id FROM chats WHERE connection=? AND uid=?',(connection,uid)).fetchone()
            if not row:
                return
            cid=row[0]
            for mid in mids:
                self.db.execute('DELETE FROM messages WHERE chat=? AND mid=?',(cid,mid))
            self.db.execute('UPDATE chats SET revision=revision+1 WHERE id=?',(cid,))
            self.db.execute("UPDATE messages SET state='stale' WHERE chat=? AND state IN ('queued','working')",(cid,))
            self.db.execute("UPDATE drafts SET state='stale' WHERE chat=? AND state='ready'",(cid,))

    def claim(self, now):
        if self.get('mode') != 'draft':
            return None
        with self.transaction():
            row=self.db.execute('''SELECT m.*,c.uid,c.connection,c.revision FROM messages m
                JOIN chats c ON c.id=m.chat JOIN connections b ON b.id=c.connection
                WHERE m.state='queued' AND c.mode='draft' AND b.enabled=1
                AND m.mid=c.latest AND m.at<=? ORDER BY m.at LIMIT 1''',(now-3,)).fetchone()
            if row:
                self.db.execute("UPDATE messages SET state='working' WHERE chat=? AND mid=?",(row['chat'],row['mid']))
                return dict(row, global_revision=int(self.get('revision')))

    def still_current(self, job):
        c=self.chat(job['chat'])
        b=self.db.execute('SELECT * FROM connections WHERE id=?',(c['connection'],)).fetchone()
        return (self.get('mode')=='draft' and c['mode']=='draft' and b and b['enabled']
                and c['revision']==job['revision'] and c['latest']==job['mid']
                and int(self.get('revision'))==job['global_revision'])

    def finish(self, job, reply, summary, handoff):
        with self.transaction():
            if not self.still_current(job):
                return None
            cur=self.db.execute('''INSERT INTO drafts(chat,mid,revision,global_revision,text,summary,handoff,created)
                VALUES (?,?,?,?,?,?,?,?)''',(job['chat'],job['mid'],job['revision'],job['global_revision'],reply,summary,int(handoff),time.time()))
            self.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=?",(job['chat'],job['mid']))
            return cur.lastrowid

    def reserve_call(self, day, limit):
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO quota(day) VALUES (?)',(day,))
            count=self.db.execute('SELECT calls FROM quota WHERE day=?',(day,)).fetchone()[0]
            if count>=limit:
                return False
            self.db.execute('UPDATE quota SET calls=calls+1 WHERE day=?',(day,))
            return True

    def reserve_send(self, did, now):
        with self.transaction():
            d=self.db.execute('SELECT * FROM drafts WHERE id=?',(did,)).fetchone()
            if not d or d['state']!='ready' or not self.still_current(d):
                return None
            c=self.chat(d['chat'])
            b=self.db.execute('SELECT * FROM connections WHERE id=?',(c['connection'],)).fetchone()
            if not b['can_reply'] or not 0<=now-c['inbound']<86340 or not 0<=now-d['created']<900:
                return None
            self.db.execute("UPDATE drafts SET state='sending' WHERE id=?",(did,))
            return dict(d, connection=c['connection'], uid=c['uid'])

    def recover(self):
        # A process crash during a paid request or send is NOT safe to replay.
        self.db.execute("UPDATE messages SET state='failed' WHERE state='working'")
        self.db.execute("UPDATE drafts SET state='uncertain' WHERE state='sending'")

    def history(self, cid):
        rows=self.db.execute('SELECT role,text FROM messages WHERE chat=? ORDER BY mid DESC LIMIT 16',(cid,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def cleanup(self, before):
        with self.transaction():
            self.db.execute('DELETE FROM messages WHERE at<?',(before,))
            self.db.execute('DELETE FROM drafts WHERE created<?',(before,))
