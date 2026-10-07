"""Business execution failures exercised with fresh local data, no real model calls."""
import asyncio
import json
import shutil
import subprocess
import time

import httpx
import pytest

from goalkeeper.codex import CODEX, task_permissions
from goalkeeper.evaluation import execute_until_stopped, verified_models
from goalkeeper.models import Task
from goalkeeper.native import Native
from goalkeeper.paths import PROJECT
from goalkeeper.sandbox import runtime_roots
from goalkeeper.transport import MeteredTransport
from test_transport_reliability import Auth, Wire, body, complete, event, route


def resilient(tmp_path, handler, retries=1):
    return MeteredTransport(tmp_path / 'auth', tmp_path / 'usage.json', auth=Auth(),
                            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                            resilient=True, transient_retries=retries, per_run_limit=None)


async def test_each_request_can_recover_and_unknown_usage_stays_visible(tmp_path):
    n = 0
    def handle(request):
        nonlocal n
        n += 1
        return httpx.Response(200, stream=Wire(break_stream=n % 2 == 1))
    meter = resilient(tmp_path, handle)
    request = route(meter, deadline=time.time()+10)
    try:
        for _ in range(2):
            raw = await body(await meter.response('one', request))
            assert raw.count(b'discard-me') == 1 and raw.count(b'response.completed') == 1
        assert n == 4 and meter.retry_used == {'one': 2}
        assert verified_models(meter.records) and meter.known() == 34
        assert sum(not c['usage_known'] for c in meter.records) == 2
        assert len({c['request_id'] for c in meter.records}) == 2
        assert not meter.stopped_reason('one')
    finally:
        await meter.close()


async def test_exhausted_group_does_not_stop_other_three_or_their_watcher(tmp_path):
    def handle(request):
        content = json.loads(request.content)['input'][0]['content']
        if content == 'pi':
            return httpx.Response(503, json={'error': {'code': 'server_error'}})
        return httpx.Response(200, content=event(complete()))
    meter = resilient(tmp_path, handle)
    tasks = []
    try:
        for group in ['pi', 'goalkeeper', 'codex', 'opencode']:
            request = route(meter, group, time.time()+10)
            async def payload(group=group):
                return {'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'xhigh'},
                        'input': [{'role': 'user', 'content': group}]}
            request.json = payload
            async def run(group=group, request=request):
                return await body(await meter.response(group, request))
            tasks.append(run())
        output = await asyncio.gather(*tasks)
        assert b'server_error' in output[0]
        assert all(b'response.completed' in raw for raw in output[1:])
        assert meter.run_reasons == {'pi': 'shared_service'} and not meter.stop.is_set()
        assert all(meter.admit(g) for g in ['goalkeeper', 'codex', 'opencode'])
        assert not meter.admit('pi')
        async def pending():
            await asyncio.sleep(.02)
            return True
        assert await execute_until_stopped(pending(), meter, 'codex', time.time()+1, .01)
        async def forever():
            await asyncio.Event().wait()
        assert not await execute_until_stopped(forever(), meter, 'pi', time.time()+1, .01)
    finally:
        await meter.close()


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
async def test_transient_http_rejection_recovers_before_native_sees_error(tmp_path, status):
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return (httpx.Response(status, json={'error': {'code': 'server_error'}}) if len(calls)==1
                else httpx.Response(200, content=event(complete())))
    meter = resilient(tmp_path, handle)
    try:
        raw = await body(await meter.response('one', route(meter, deadline=time.time()+10)))
        assert b'response.completed' in raw and b'server_error' not in raw
        assert calls[0] == calls[1] and verified_models(meter.records)
        assert not meter.stop.is_set() and not meter.run_reasons
        assert not meter.records[0]['usage_known']  # Do not claim a rejection cost zero.
    finally:
        await meter.close()


@pytest.mark.parametrize('status,code', [(401, 'authentication_error'), (429, 'quota_exceeded')])
async def test_authentication_and_quota_still_stop_all_without_retry(tmp_path, status, code):
    meter = resilient(tmp_path, lambda r:httpx.Response(status,json={'error':{'code':code}}), retries=3)
    try:
        await body(await meter.response('one', route(meter)))
        route(meter, 'two')
        assert meter.stop.is_set() and not meter.admit('two')
        assert len(meter.records)==1 and not meter.retry_used
    finally:
        await meter.close()


def test_failed_or_wrong_model_response_cannot_be_misclassified_as_recovery():
    failed={'model':'gpt-6.1-sol','effort':'xhigh','status':'transport_error',
            'retry_scheduled':True,'request_id':'same','usage_known':False}
    delivered={**failed,'status':'completed','returned_model':'gpt-6.1-sol','forwarded':True}
    assert verified_models([failed,delivered])
    assert not verified_models([failed])
    assert not verified_models([failed,{**delivered,'request_id':'different'}])
    assert not verified_models([failed,{**delivered,'returned_model':'other'}])
    assert not verified_models([failed,{**delivered,'forwarded':False}])
    assert not verified_models([{**failed,'returned_model':'other'},delivered])


async def test_failed_response_with_wrong_model_cannot_retry_or_publish(tmp_path):
    terminal={'type':'response.failed','response':{'model':'other','error':{'code':'server_error'}}}
    meter=resilient(tmp_path,lambda r:httpx.Response(200,stream=Wire(terminal=terminal)),retries=3)
    try:
        raw=await body(await meter.response('one',route(meter)))
        assert b'discard-me' not in raw and len(meter.records)==1 and not meter.retry_used
        assert meter.reason=='returned_model_mismatch' and not verified_models(meter.records)
    finally:
        await meter.close()


def test_four_business_packages_save_with_actual_pinned_codex_permissions(tmp_path):
    commands={'B1-v2':('badcase','diagnose'),'B3-v2':('experiments','analyze'),
              'B6-v2':('retirement','plan'),'B11-v2':('receipts','apply')}
    for tid,(module,command) in commands.items():
        package=PROJECT/'evaluation/private'/tid
        root=tmp_path/tid/'workspace'
        shutil.copytree(package/'reference',root)
        task=Task.model_validate_json((package/'task.json').read_text())
        profile,env=task_permissions(root,runtime_roots(),task.write_paths,task.protected_paths)
        home=tmp_path/tid/'codex-home'
        home.mkdir()
        env['CODEX_HOME']=str(home)
        target=root/'artifacts/analysis.json'
        expected=json.loads(target.read_text())
        target.unlink()
        argv=[str(CODEX),'-c','permissions.goalkeeper-evaluation='+profile,
              '-c','approval_policy="never"','sandbox','-P','goalkeeper-evaluation','-C',str(root),'--',
              str(PROJECT/'.venv/bin/python'),'-B','-m',module+'.cli',command,'data/sample.json',
              '--save','artifacts/analysis.json']
        for _ in range(2):
            result=subprocess.run(argv,cwd=root,env=env,capture_output=True,text=True,timeout=30)
            assert result.returncode==0,(tid,result.stdout,result.stderr)
            assert json.loads(result.stdout)==expected and json.loads(target.read_text())==expected
        denied=subprocess.run([*argv[:-1],'unauthorized.json'],cwd=root,env=env,
                              capture_output=True,text=True,timeout=30)
        assert denied.returncode==2 and not (root/'unauthorized.json').exists()


async def test_pinned_codex_recovers_http_503_without_a_blocked_native_turn(tmp_path, monkeypatch):
    from goalkeeper.codex import CodexClient
    from test_pinned_offline import SlowResponse
    directory=tmp_path/'run'
    root=directory/'workspace'
    root.mkdir(parents=True)
    task=Task(id='offline',objective='Complete the synthetic fixture.',constraints=[],
              acceptance=['synthetic'],write_paths=['result.txt'],
              checks=[{'id':'ready','argv':['/bin/true'],'timeout':1}])
    sent=[]
    def upstream(request):
        sent.append(json.loads(request.content))
        if len(sent)==1:
            return httpx.Response(503,json={'error':{'code':'server_error'}})
        return httpx.Response(200,stream=SlowResponse())
    meter=resilient(tmp_path,upstream)
    await meter.start()
    from types import SimpleNamespace
    registered=meter.register(SimpleNamespace(id='codex',deadline=time.time()+20,
                                              config=SimpleNamespace(engine='codex')))
    class Consumer(CodexClient):
        def use_subscription_transport(self,*args):
            super().use_subscription_transport(*args)
            self.transport_overrides.update(HTTP_PROXY='',HTTPS_PROXY='',ALL_PROXY='',
                                            NO_PROXY='127.0.0.1,localhost')
        async def set_goal(self,*args):
            # Goal completion is synthetic; the pinned binary still consumes the actual recovered wire.
            await self.events.put({'method':'thread/goal/updated','params':{
                'threadId':self.thread_id,'goal':{'status':'complete'}}})
    monkeypatch.setattr('goalkeeper.native.CodexClient',Consumer)
    adapter=Native('codex',directory,task,root,registered['DIAGNOSTIC_TRANSPORT_BASE'],
                   registered['LOCAL_ROUTE_TOKEN'],seconds=20)
    try:
        assert await adapter.run(),(directory/'native-result.json').read_text()
        assert len(sent)==2 and sent[0]==sent[1]
        assert '不要重复调用create_goal' in json.dumps(sent[0],ensure_ascii=False)
        assert verified_models(meter.records) and not meter.run_reasons
    finally:
        await meter.close()
