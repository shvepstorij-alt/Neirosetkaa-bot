"""Opt-in authenticated API for fixed synthetic tests only, no customer operations."""
import asyncio
import hmac
import json
import os
import re
import time
import hashlib
from pathlib import Path


def code_fingerprint():
    root=Path(__file__).resolve().parent
    files=sorted(p.name for p in root.glob('*.py') if p.name!='probe_client.py')
    files+=['STYLE.md','SERVICE_RULES.md','WORKFLOW_RULES.md','requirements.txt']
    # Compare code/rules, not Windows vs Linux line-ending conventions.
    return {name:hashlib.sha256((root/name).read_text(encoding='utf-8').replace('\r\n','\n').encode('utf-8')).hexdigest()
            for name in files}


class ProbeConfigurationError(ValueError):
    """Controlled diagnostics only; never includes variable contents."""


def number_setting(name,default,minimum,maximum):
    raw=os.getenv(name,str(default)).strip()
    if not re.fullmatch(r'[0-9]{1,6}',raw):
        raise ProbeConfigurationError(name+': требуется целое число; значение скрыто.')
    value=int(raw)
    if not minimum<=value<=maximum:
        raise ProbeConfigurationError(name+f': допустимый диапазон {minimum}–{maximum}; значение скрыто.')
    return value


def startup_diagnostic(exc):
    if isinstance(exc,ProbeConfigurationError):return str(exc)
    return 'Первопричина не установлена. Тип='+type(exc).__name__+'; проверьте подключение HTTP-порта и состояние хранилища.'


class ProbeManager:
    def __init__(self,service):
        self.service=service
        self.db=service.store.db
        self.build=code_fingerprint()
        self.db.execute('''CREATE TABLE IF NOT EXISTS remote_probe_runs(
            id TEXT PRIMARY KEY,created REAL,status TEXT,data TEXT)''')
        self.db.execute("UPDATE remote_probe_runs SET status='interrupted' WHERE status IN ('queued','running')")

    def read(self,ident):
        self.db.execute('DELETE FROM remote_probe_runs WHERE created<?',(time.time()-86400,))
        row=self.db.execute('SELECT * FROM remote_probe_runs WHERE id=?',(ident,)).fetchone()
        if not row:return None
        return dict(id=row['id'],created=row['created'],status=row['status'],**json.loads(row['data']))

    def start(self,ident,suite='baseline'):
        from .coverage_cases import SUITES
        if suite not in ('baseline','acceptance',*SUITES):return {'error':'unknown_test_suite'},400
        existing=self.read(ident)
        if existing:
            if existing.get('suite','baseline')!=suite:return {'error':'test_suite_conflict'},409
            return existing,200
        running=getattr(self.service,'research_probe_task',None)
        if running and not running.done():return {'error':'test_already_running'},409
        self.db.execute('INSERT INTO remote_probe_runs VALUES (?,?,?,?)',
                        (ident,time.time(),'queued',json.dumps({'reports':[],'calls':0,'build':self.build,'suite':suite})))
        self.service.research_probe_task=asyncio.create_task(self._run(ident,self.service.store.get('revision'),suite))
        return self.read(ident),202

    async def _run(self,ident,revision,suite='baseline'):
        from .dialog_probe import run_probe
        reports=[]
        def save(status,**extra):
            self.db.execute('UPDATE remote_probe_runs SET status=?,data=? WHERE id=?',
                (status,json.dumps(dict(reports=reports,build=self.build,suite=suite,**extra),ensure_ascii=False),ident))
        async def emit(text):
            if len(reports)>=10 or len(text)>24000:raise ValueError('Test report capacity exceeded')
            reports.append(text)
            save('running')
        save('running')
        try:
            options={'suite':suite} if suite!='baseline' else {}
            result=await asyncio.wait_for(run_probe(self.service,max_calls=20,emit=emit,expected_revision=revision,**options),timeout=1200)
            result=dict(result or {'status':'failed','reason':'runner_returned_no_result'})
            status=result.pop('status','failed')
            save(status,**result)
        except asyncio.CancelledError:
            save('cancelled',reason='test_cancelled; in-flight API request may still be billed')
            raise
        except TimeoutError:
            save('failed',reason='total_test_timeout_1200_seconds')
        except Exception as exc:
            save('failed',reason='test_runner_exception',exception_type=type(exc).__name__)


def create_app(service,token):
    from aiohttp import web
    token=token.strip()
    if not token:
        raise ProbeConfigurationError('SUPPORT_PROBE_TOKEN: переменная отсутствует или пуста. Нужен отдельный тестовый ключ.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',token):
        raise ProbeConfigurationError('SUPPORT_PROBE_TOKEN: неверный формат. Длина='+str(len(token))
            +'; нужно 43–128 символов A–Z, a–z, 0–9, _ или -. Ключ скрыт.')
    manager=ProbeManager(service)

    @web.middleware
    async def authorize(request,handler):
        supplied=request.headers.get('Authorization','')
        if not hmac.compare_digest(supplied.encode(),('Bearer '+token).encode()):
            return web.json_response({'error':'unauthorized'},status=401,headers={'Cache-Control':'no-store'})
        response=await handler(request)
        response.headers['Cache-Control']='no-store'
        return response

    async def start(request):
        try:data=await request.json()
        except Exception:return web.json_response({'error':'expected_request_id_only'},status=400)
        if (not isinstance(data,dict) or set(data)!={'request_id'}
                or not isinstance(data['request_id'],str) or not re.fullmatch(r'[a-f0-9]{32}',data['request_id'])):
            return web.json_response({'error':'expected_request_id_only'},status=400)
        suite=request.path.rsplit('/',1)[-1]
        result,status=manager.start(data['request_id'],suite='baseline' if suite=='tests' else suite)
        return web.json_response(result,status=status)

    async def read(request):
        ident=request.match_info['ident']
        if not re.fullmatch(r'[a-f0-9]{32}',ident):return web.json_response({'error':'invalid_id'},status=400)
        result=manager.read(ident)
        return web.json_response(result if result else {'error':'not_found'},status=200 if result else 404)

    async def info(request):
        import datetime as dt
        now=dt.datetime.now(dt.timezone.utc)
        row=service.store.db.execute('SELECT calls FROM quota WHERE day=?',(now.date().isoformat(),)).fetchone()
        used=int(row[0]) if row else 0
        limit=service.daily_limit
        quota={'limit':limit,'used':used,'remaining':max(0,limit-used),
               'resets_at_utc':dt.datetime.combine(now.date()+dt.timedelta(days=1),dt.time(),dt.timezone.utc).isoformat()}
        task=getattr(service,'research_probe_task',None)
        return web.json_response({'build':manager.build,'daily_run_limit':None,'daily_run_limit_enabled':False,
                                  'daily_api_quota':quota,
                                  'test_running':bool(task and not task.done())})

    app=web.Application(middlewares=[authorize],client_max_size=512)
    app.router.add_post('/v1/tests',start)
    app.router.add_post('/v1/tests/acceptance',start)
    from .coverage_cases import SUITES
    for suite in SUITES:
        app.router.add_post('/v1/tests/'+suite,start)
    app.router.add_get('/v1/tests',info)
    app.router.add_get('/v1/tests/{ident}',read)
    return app


async def start_server(service):
    if os.getenv('SUPPORT_PROBE_ENABLED')!='yes':return None
    from aiohttp import web
    port=number_setting('PORT',8080,1,65535)
    app=create_app(service,os.getenv('SUPPORT_PROBE_TOKEN',''))
    runner=web.AppRunner(app,access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner,'0.0.0.0',port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
