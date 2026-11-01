"""Opt-in authenticated API for fixed synthetic tests only, no customer operations."""
import asyncio
import datetime as dt
import hmac
import json
import os
import re
import time


class ProbeManager:
    def __init__(self,service,daily_runs=3):
        self.service=service
        self.db=service.store.db
        self.daily_runs=daily_runs
        self.db.execute('''CREATE TABLE IF NOT EXISTS remote_probe_runs(
            id TEXT PRIMARY KEY,created REAL,status TEXT,data TEXT)''')
        self.db.execute("UPDATE remote_probe_runs SET status='interrupted' WHERE status IN ('queued','running')")

    def read(self,ident):
        self.db.execute('DELETE FROM remote_probe_runs WHERE created<?',(time.time()-86400,))
        row=self.db.execute('SELECT * FROM remote_probe_runs WHERE id=?',(ident,)).fetchone()
        if not row:return None
        return dict(id=row['id'],created=row['created'],status=row['status'],**json.loads(row['data']))

    def start(self,ident):
        existing=self.read(ident)
        if existing:return existing,200
        running=getattr(self.service,'research_probe_task',None)
        if running and not running.done():return {'error':'test_already_running'},409
        day=dt.datetime.now(dt.timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        used=self.db.execute('SELECT count(*) FROM remote_probe_runs WHERE created>=?',(day,)).fetchone()[0]
        if used>=self.daily_runs:return {'error':'daily_test_limit','limit':self.daily_runs},429
        self.db.execute('INSERT INTO remote_probe_runs VALUES (?,?,?,?)',
                        (ident,time.time(),'queued',json.dumps({'reports':[],'calls':0})))
        self.service.research_probe_task=asyncio.create_task(self._run(ident,self.service.store.get('revision')))
        return self.read(ident),202

    async def _run(self,ident,revision):
        from .dialog_probe import run_probe
        reports=[]
        def save(status,**extra):
            self.db.execute('UPDATE remote_probe_runs SET status=?,data=? WHERE id=?',
                (status,json.dumps(dict(reports=reports,**extra),ensure_ascii=False),ident))
        async def emit(text):
            if len(reports)>=10 or len(text)>24000:raise ValueError('Test report capacity exceeded')
            reports.append(text)
            save('running')
        save('running')
        try:
            result=await asyncio.wait_for(run_probe(self.service,max_calls=20,emit=emit,expected_revision=revision),timeout=1200)
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


def create_app(service,token,daily_runs=3):
    from aiohttp import web
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',token):
        raise ValueError('SUPPORT_PROBE_TOKEN must be a separate random token of 43-128 URL-safe characters')
    if not 1<=daily_runs<=10:raise ValueError('SUPPORT_PROBE_DAILY_RUNS must be 1-10')
    manager=ProbeManager(service,daily_runs)

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
        result,status=manager.start(data['request_id'])
        return web.json_response(result,status=status)

    async def read(request):
        ident=request.match_info['ident']
        if not re.fullmatch(r'[a-f0-9]{32}',ident):return web.json_response({'error':'invalid_id'},status=400)
        result=manager.read(ident)
        return web.json_response(result if result else {'error':'not_found'},status=200 if result else 404)

    app=web.Application(middlewares=[authorize],client_max_size=512)
    app.router.add_post('/v1/tests',start)
    app.router.add_get('/v1/tests/{ident}',read)
    return app


async def start_server(service):
    if os.getenv('SUPPORT_PROBE_ENABLED')!='yes':return None
    from aiohttp import web
    app=create_app(service,os.getenv('SUPPORT_PROBE_TOKEN',''),int(os.getenv('SUPPORT_PROBE_DAILY_RUNS','3')))
    runner=web.AppRunner(app,access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner,'0.0.0.0',int(os.getenv('PORT','8080'))).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
