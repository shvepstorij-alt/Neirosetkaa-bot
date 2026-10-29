"""Durable, single-process test service state. Never imports the shop database."""
import json
import sqlite3
import time
from contextlib import contextmanager

HISTORY_MESSAGES = 60
HISTORY_CHARACTERS = 24000
HISTORY_TRUNCATED = '[Начало сообщения не включено в контекст]\n'


class Store:
    def __init__(self, path, batch_seconds=300):
        self.batch_seconds=batch_seconds
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
          CREATE TABLE IF NOT EXISTS draft_evidence(
            draft INTEGER PRIMARY KEY, data TEXT);
          CREATE INDEX IF NOT EXISTS messages_queue ON messages(state,at,chat);
          CREATE INDEX IF NOT EXISTS drafts_state ON drafts(state,id);
          CREATE INDEX IF NOT EXISTS chats_inbound ON chats(inbound DESC,id);
        ''')

        columns={row['name'] for row in self.db.execute('PRAGMA table_info(chats)')}
        for name,kind in (('batch_due','REAL'),('batch_start','REAL'),('batch_first','INTEGER')):
            if name not in columns:
                self.db.execute(f'ALTER TABLE chats ADD COLUMN {name} {kind} NOT NULL DEFAULT 0')
        # Existing queued work receives a persistent collection window on upgrade.
        self.db.execute("""UPDATE chats SET batch_start=?,batch_due=?,batch_first=latest
            WHERE batch_due=0 AND EXISTS (SELECT 1 FROM messages m
            WHERE m.chat=chats.id AND m.mid=chats.latest AND m.state='queued')""",
            (time.time(),time.time()+self.batch_seconds))

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
            # Stale resume buttons must not invalidate an already active draft.
            chat=self.chat(cid) if cid is not None else None
            if cid is not None and chat is None:
                raise ValueError('Unknown chat')
            current=chat['mode'] if chat is not None else self.get('mode')
            if mode=='draft' and current=='draft':
                return False
            if cid is None:
                self.put('mode', mode)
                self.put('revision', int(self.get('revision')) + 1)
                self.db.execute("UPDATE messages SET state='stale' WHERE state IN ('queued','working')")
                self.db.execute("UPDATE drafts SET state='stale' WHERE state='ready'")
            else:
                self.db.execute('UPDATE chats SET mode=?,revision=revision+1 WHERE id=?', (mode,cid))
                self.db.execute("UPDATE messages SET state='stale' WHERE chat=? AND state IN ('queued','working')", (cid,))
                self.db.execute("UPDATE drafts SET state='stale' WHERE chat=? AND state='ready'", (cid,))
        return True

    def connection(self, ident, enabled, can_reply):
        with self.transaction():
            previous = self.db.execute('SELECT * FROM connections WHERE id=?', (ident,)).fetchone()
            self.db.execute('INSERT OR REPLACE INTO connections VALUES (?,?,?)',
                            (ident, int(enabled), int(can_reply)))
            if previous and (previous['enabled'] != int(enabled) or previous['can_reply'] != int(can_reply)):
                self.db.execute('UPDATE chats SET revision=revision+1 WHERE connection=?', (ident,))
                self.db.execute("UPDATE messages SET state='stale' WHERE chat IN (SELECT id FROM chats WHERE connection=?) AND state IN ('queued','working')",(ident,))
                self.db.execute("UPDATE drafts SET state='stale' WHERE chat IN (SELECT id FROM chats WHERE connection=?) AND state='ready'",(ident,))

    def ingest(self, connection, uid, mid, role, text, media, at, edited=False):
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO chats(connection,uid) VALUES (?,?)', (connection,uid))
            chat = self.db.execute('SELECT * FROM chats WHERE connection=? AND uid=?', (connection,uid)).fetchone()
            cid = chat['id']
            existing = self.db.execute('SELECT 1 FROM messages WHERE chat=? AND mid=?', (cid,mid)).fetchone()
            if existing and not edited:
                return cid
            pending=self.db.execute("SELECT state FROM messages WHERE chat=? AND mid=?",
                                    (cid,chat['latest'])).fetchone()
            collecting=bool(pending and pending['state']=='queued' and chat['batch_due']>0)
            # Any manual owner message or edit invalidates pending suggestions.
            if role == 'owner':
                self.db.execute("UPDATE chats SET mode='off',revision=revision+1 WHERE id=?", (cid,))
            else:
                self.db.execute('UPDATE chats SET latest=MAX(latest,?),inbound=MAX(inbound,?),revision=revision+1 WHERE id=?', (mid,at,cid))
            self.db.execute("UPDATE messages SET state='stale' WHERE chat=? AND state IN ('queued','working')", (cid,))
            self.db.execute("UPDATE drafts SET state='stale' WHERE chat=? AND state='ready'", (cid,))
            chat = self.chat(cid)
            queued = role == 'client' and mid == chat['latest'] and chat['mode']=='draft' and self.get('mode')=='draft'
            if queued and not collecting:
                start=time.time() if edited else min(float(at),time.time())
                self.db.execute('UPDATE chats SET batch_start=?,batch_due=?,batch_first=? WHERE id=?',
                                (start,start+self.batch_seconds,mid,cid))
            self.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)',
                            (cid,mid,role,text,json.dumps(media),at,'queued' if queued else 'stored'))
            if role=='client' and mid!=chat['latest'] and pending and pending['state'] in ('queued','working') and chat['mode']=='draft' and self.get('mode')=='draft':
                # Editing an earlier fragment must not strand the last fragment.
                self.db.execute("UPDATE messages SET state='queued' WHERE chat=? AND mid=? AND role='client'",
                                (cid,chat['latest']))
                if not collecting:
                    self.db.execute('UPDATE chats SET batch_start=?,batch_due=? WHERE id=?',
                                    (time.time(),time.time()+self.batch_seconds,cid))
            return cid

    def delete_messages(self, connection, uid, mids):
        with self.transaction():
            row=self.db.execute('SELECT id FROM chats WHERE connection=? AND uid=?',(connection,uid)).fetchone()
            if not row:
                return
            cid=row[0]
            c=self.chat(cid)
            pending=self.db.execute('SELECT state FROM messages WHERE chat=? AND mid=?',
                                    (cid,c['latest'])).fetchone()
            for mid in mids:
                self.db.execute('DELETE FROM messages WHERE chat=? AND mid=?',(cid,mid))
            self.db.execute('UPDATE chats SET revision=revision+1 WHERE id=?',(cid,))
            self.db.execute("UPDATE messages SET state='stale' WHERE chat=? AND state IN ('queued','working')",(cid,))
            self.db.execute("UPDATE drafts SET state='stale' WHERE chat=? AND state='ready'",(cid,))

            if pending and pending['state'] in ('queued','working') and c['mode']=='draft' and self.get('mode')=='draft':
                latest=self.db.execute("SELECT mid FROM messages WHERE chat=? AND role='client' AND mid>=? ORDER BY mid DESC LIMIT 1",
                                       (cid,c['batch_first'])).fetchone()
                if latest:
                    self.db.execute('UPDATE chats SET latest=? WHERE id=?',(latest[0],cid))
                    self.db.execute("UPDATE messages SET state='queued' WHERE chat=? AND mid=?",(cid,latest[0]))
                    if pending['state']=='working':
                        self.db.execute('UPDATE chats SET batch_start=?,batch_due=? WHERE id=?',
                                        (time.time(),time.time()+self.batch_seconds,cid))

    def claim(self, now, excluded_chats=()):
        if self.get('mode') != 'draft':
            return None
        with self.transaction():
            excluded=tuple(set(excluded_chats))
            exclusion=(' AND c.id NOT IN ('+','.join('?' for _ in excluded)+')') if excluded else ''
            row=self.db.execute('''SELECT m.*,c.uid,c.connection,c.revision FROM messages m
                JOIN chats c ON c.id=m.chat JOIN connections b ON b.id=c.connection
                WHERE m.state='queued' AND c.mode='draft' AND b.enabled=1 AND b.can_reply=1
                AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.chat=c.id AND d.state='sending')
                AND m.mid=c.latest AND c.batch_due<=?'''+exclusion+' ORDER BY m.at,m.chat LIMIT 1',
                (now,*excluded)).fetchone()
            if row:
                self.db.execute("UPDATE messages SET state='working' WHERE chat=? AND mid=?",(row['chat'],row['mid']))
                return dict(row, global_revision=int(self.get('revision')))

    def prepare_now(self,cid,now):
        """Expedite only an existing queued batch; never revive completed work."""
        with self.transaction():
            c=self.chat(cid)
            if not c or c['mode']!='draft' or self.get('mode')!='draft':return False
            b=self.db.execute('SELECT * FROM connections WHERE id=?',(c['connection'],)).fetchone()
            if not b or not b['enabled'] or not b['can_reply']:return False
            pending=self.db.execute("SELECT 1 FROM messages WHERE chat=? AND mid=? AND state='queued'",
                                    (cid,c['latest'])).fetchone()
            if not pending:return False
            self.db.execute('UPDATE chats SET batch_due=MIN(batch_due,?) WHERE id=?',(now,cid))
            return True

    def still_current(self, job):
        c=self.chat(job['chat'])
        if not c:
            return False
        b=self.db.execute('SELECT * FROM connections WHERE id=?',(c['connection'],)).fetchone()
        return (self.get('mode')=='draft' and c['mode']=='draft' and b and b['enabled'] and b['can_reply']
                and c['revision']==job['revision'] and c['latest']==job['mid']
                and int(self.get('revision'))==job['global_revision'])

    def finish(self, job, reply, summary, handoff, evidence=None):
        with self.transaction():
            if not self.still_current(job):
                return None
            pending=self.db.execute('SELECT state FROM messages WHERE chat=? AND mid=?',
                                    (job['chat'],job['mid'])).fetchone()
            if not pending or pending['state']!='working':
                return None
            cur=self.db.execute('''INSERT INTO drafts(chat,mid,revision,global_revision,text,summary,handoff,created)
                VALUES (?,?,?,?,?,?,?,?)''',(job['chat'],job['mid'],job['revision'],job['global_revision'],reply,summary,int(handoff),time.time()))
            self.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=?",(job['chat'],job['mid']))
            self.db.execute('INSERT INTO draft_evidence VALUES (?,?)',
                            (cur.lastrowid,json.dumps(evidence or {'status':'not_requested','sources':[]})))
            return cur.lastrowid

    def evidence(self,did):
        row=self.db.execute('SELECT data FROM draft_evidence WHERE draft=?',(did,)).fetchone()
        return json.loads(row[0]) if row else {'status':'not_recorded','sources':[]}

    def reserve_call(self, day, limit):
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO quota(day) VALUES (?)',(day,))
            count=self.db.execute('SELECT calls FROM quota WHERE day=?',(day,)).fetchone()[0]
            if count>=limit:
                return False
            self.db.execute('UPDATE quota SET calls=calls+1 WHERE day=?',(day,))
            return True

    def send_block_reason(self, did, now):
        """Explain current send guards without exposing customer content or secrets."""
        d=self.db.execute('SELECT * FROM drafts WHERE id=?',(did,)).fetchone()
        if not d:return 'черновик не найден или удалён по сроку хранения'
        c=self.chat(d['chat'])
        if not c:return 'чат не найден'
        if c['latest']!=d['mid']:return 'поступило новое или изменённое сообщение клиента; этот черновик относится к предыдущему обращению'
        if d['state'] not in ('ready','stale'):
            states={'sent':'черновик уже отправлен; повторная отправка заблокирована',
                    'sending':'отправка этого черновика уже выполняется',
                    'uncertain':'результат прошлой отправки неизвестен; сначала проверьте личку',
                    'stale':'черновик отменён после изменения сообщений или режима работы'}
            return states.get(d['state'],'черновик не находится в состоянии готовности к отправке')
        if self.get('mode')!='draft':return 'помощник остановлен глобально'
        if c['mode']!='draft':return 'помощник в этом чате на паузе'
        b=self.db.execute('SELECT * FROM connections WHERE id=?',(c['connection'],)).fetchone()
        if not b or not b['enabled']:return 'подключение Telegram Business отсутствует или отключено'
        if not b['can_reply']:return 'Telegram Business не предоставляет право ответа в этом чате'
        if d['state']=='stale':return 'черновик отменён после изменения сообщений или режима работы'
        if c['revision']!=d['revision'] or int(self.get('revision'))!=d['global_revision']:
            return 'после подготовки черновика изменился режим работы; подготовьте новый ответ'
        if self.db.execute("SELECT 1 FROM drafts WHERE chat=? AND state='sending' LIMIT 1",(d['chat'],)).fetchone():
            return 'в этом чате уже отправляется другой ответ'
        if not 0<=now-c['inbound']<86340:return 'истекло допустимое окно ответа Telegram либо время последнего обращения некорректно'
        if not 0<=now-d['created']<900:return 'истёк срок проверки черновика (15 минут) либо время его создания некорректно'
        return 'первопричина отказа не установлена; требуется проверка состояния отправки'

    def reserve_send(self, did, now):
        with self.transaction():
            d=self.db.execute('SELECT * FROM drafts WHERE id=?',(did,)).fetchone()
            if not d or d['state']!='ready' or not self.still_current(d):
                return None
            if self.db.execute("SELECT 1 FROM drafts WHERE chat=? AND state='sending' LIMIT 1",(d['chat'],)).fetchone():
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
        for row in self.db.execute("SELECT DISTINCT chat FROM drafts WHERE state='uncertain'").fetchall():
            self.switch('off',row['chat'])

    def history(self, cid):
        # Recent context is bounded in both turns and characters, per chat.
        # Preserve newest messages first; never silently splice unrelated chats.
        rows=self.db.execute('SELECT role,text FROM messages WHERE chat=? ORDER BY mid DESC LIMIT ?',
                             (cid,HISTORY_MESSAGES)).fetchall()
        chosen=[];remaining=HISTORY_CHARACTERS
        for row in rows:
            text=row['text'] or ''
            if len(text)>remaining:
                if not chosen:
                    text=HISTORY_TRUNCATED+text[-(remaining-len(HISTORY_TRUNCATED)):]
                    chosen.append({'role':row['role'],'text':text})
                break
            chosen.append({'role':row['role'],'text':text})
            remaining-=len(text)
            if remaining<=0:
                break
        return list(reversed(chosen))

    def cleanup(self, before):
        with self.transaction():
            self.db.execute('DELETE FROM messages WHERE at<?',(before,))
            self.db.execute('DELETE FROM drafts WHERE created<?',(before,))
            self.db.execute('DELETE FROM draft_evidence WHERE draft NOT IN (SELECT id FROM drafts)')
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plural_context_refs'").fetchone():
                self.db.execute("DELETE FROM plural_context_refs WHERE NOT EXISTS (SELECT 1 FROM messages WHERE messages.chat=plural_context_refs.chat AND messages.mid=plural_context_refs.mid AND role='client')")
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_context_refs'").fetchone():
                self.db.execute("DELETE FROM session_context_refs WHERE NOT EXISTS (SELECT 1 FROM messages WHERE messages.chat=session_context_refs.chat AND messages.mid=session_context_refs.mid AND role='client')")
