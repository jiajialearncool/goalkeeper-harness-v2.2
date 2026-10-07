"""Run the pinned native executable against fresh local fixtures; no real inference."""
import asyncio
import json
import subprocess
import sys
import time

import httpx
import pytest

from goalkeeper.codex import CODEX, CodexClient, task_permissions
from goalkeeper.models import Task
from goalkeeper.native import Native
from goalkeeper.paths import PROJECT
from goalkeeper.sandbox import Scope, Seatbelt, runtime_roots
from goalkeeper.transport import MeteredTransport
from test_transport_reliability import Auth, event

pytestmark = pytest.mark.skipif(sys.platform != 'darwin',reason='Requires macOS native sandbox')


def test_scoped_pinned_codex_permissions_without_inference(tmp_path):
    workspace = tmp_path/'task'
    workspace.mkdir()
    (workspace/'tickets').mkdir()
    (workspace/'tickets/check.py').write_text('protected')
    probe = workspace/'probe.py'
    probe.write_text("from pathlib import Path\n"
        "Path('result.txt').write_text('READY')\n"
        "Path('tickets/newdir').mkdir()\n"
        "Path('tickets/newdir/solution.py').write_text('OK')\n"
        "def denied(path):\n"
        "    try: Path(path).write_text('bad')\n"
        "    except PermissionError: return True\n"
        "    return False\n"
        "assert denied('outside.txt')\n"
        "assert denied('tickets/check.py')\n"
        "print('SCOPED_PERMISSIONS_PASSED')\n")
    profile, env = task_permissions(workspace,runtime_roots(),
        write_paths=['result.txt','tickets/'],protected_paths=['tickets/check.py'])
    env['CODEX_HOME'] = str(tmp_path/'codex-home')
    (tmp_path/'codex-home').mkdir()
    result = subprocess.run([str(CODEX),'-c','permissions.goalkeeper-evaluation='+profile,
        '-c','approval_policy="never"','sandbox','-P','goalkeeper-evaluation','-C',str(workspace),
        '--',str(PROJECT/'.venv/bin/python'),str(probe)],cwd=workspace,env=env,
        capture_output=True,text=True,timeout=30)
    assert result.returncode == 0,result.stdout+result.stderr
    assert 'SCOPED_PERMISSIONS_PASSED' in result.stdout
    assert (workspace/'tickets/check.py').read_text() == 'protected'
    assert not (workspace/'outside.txt').exists()


async def test_spawn_callback_failure_does_not_leak_sandbox_process(tmp_path):
    workspace = tmp_path/'task'
    workspace.mkdir()
    runner = Seatbelt(Scope(workspace,runtimes=runtime_roots()))
    spawned = []
    def fail(process):
        spawned.append(process)
        raise OSError('synthetic checkpoint failure')
    runner.on_spawn = fail
    with pytest.raises(OSError,match='synthetic'):
        await runner.run(['/bin/sleep','20'],30)
    assert spawned and spawned[0].returncode is not None
    assert not runner.processes


class SlowResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        base = {'id':'resp_offline','object':'response','created_at':1,
            'model':'gpt-6.1-sol','status':'in_progress','output':[]}
        yield event({'type':'response.created','response':base,'sequence_number':0})
        # Longer than a synthetic old native idle timeout. Staging hides semantic
        # events until completion, exactly as production forwarding does.
        await asyncio.sleep(.5)
        item = {'id':'msg_offline','type':'message','role':'assistant','status':'completed',
                'content':[{'type':'output_text','text':'Offline fixture complete.','annotations':[]}]}
        yield event({'type':'response.output_item.done','output_index':0,'item':item,'sequence_number':1})
        yield event({'type':'response.completed','sequence_number':2,'response':{
            **base,'status':'completed','output':[item],
            'usage':{'input_tokens':10,'output_tokens':7,'total_tokens':17,
                'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}})

    async def aclose(self):
        pass


async def test_pinned_codex_consumes_slow_staged_response_without_inference(tmp_path,monkeypatch):
    directory = tmp_path/'run'
    workspace = directory/'workspace'
    workspace.mkdir(parents=True)
    task = Task(id='offline',objective='Complete the synthetic local fixture.',constraints=[],
        acceptance=['synthetic fixture'],write_paths=['result.txt'],
        checks=[{'id':'ready','argv':['/bin/true'],'timeout':1}])
    sent = []
    def upstream(request):
        sent.append(request)
        return httpx.Response(200,stream=SlowResponse())
    meter = MeteredTransport(tmp_path/'auth',tmp_path/'usage.json',auth=Auth(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    await meter.start()
    route = meter.register(type('State',(),{'id':'codex','deadline':time.time()+15,
                    'config':type('Config',(),{'engine':'codex'})()})())
    clients = []
    class LocalConsumer(CodexClient):
        def use_subscription_transport(self,*args):
            super().use_subscription_transport(*args)
            self.provider_settings['model_providers.openai_chatgpt_plan.stream_idle_timeout_ms']='100'
            self.provider_settings['model_providers.openai_chatgpt_plan.stream_max_retries']='0'
            self.transport_overrides.update(HTTP_PROXY='',HTTPS_PROXY='',ALL_PROXY='',
                                            NO_PROXY='127.0.0.1,localhost')
        async def start(self):
            assert json.loads(self.provider_settings['model_providers.openai_chatgpt_plan.base_url']).startswith('http://127.0.0.1:')
            assert int(self.provider_settings['model_providers.openai_chatgpt_plan.stream_idle_timeout_ms']) > 1000
            clients.append(self)
            result = await super().start()
            cfg = (await self.request('config/read',{'includeLayers':False}))['config']
            assert cfg['model_providers']['openai_chatgpt_plan']['stream_idle_timeout_ms'] > 1000
            return result
        async def set_goal(self,*args):
            # Only completion notification is synthetic. The actual pinned
            # process consumes real SSE/RPC wire frames and publishes turn/end.
            await self.events.put({'method':'thread/goal/updated','params':{
                'threadId':self.thread_id,'goal':{'status':'complete'}}})
    monkeypatch.setattr('goalkeeper.native.CodexClient',LocalConsumer)
    adapter = Native('codex',directory,task,workspace,route['DIAGNOSTIC_TRANSPORT_BASE'],
                     route['LOCAL_ROUTE_TOKEN'],seconds=15)
    try:
        assert await adapter.run(),(directory/'native-result.json').read_text()
        assert clients and clients[0].process.returncode is not None
        assert len(sent) == 1 and meter.known() == 17 and meter.reserved() == 0
        assert not meter.stop.is_set()
        assert json.loads((directory/'native-result.json').read_text())['cleanup_complete']
    finally:
        await meter.close()


async def test_frozen_c1_grader_accepts_reference_and_rejects_critical_errors(tmp_path):
    import shutil
    from goalkeeper.evaluation import score
    from goalkeeper.storage import snapshot, save_report
    package = PROJECT/'evaluation/private/C1'
    before = snapshot(package)
    versions = {
        'reference':None,
        'mutates_input':('service.py','out=deepcopy(state)','out=state'),
        'no_idempotency':('service.py',"if batch_id in out['batches']:",'if False:'),
        'blank_erases_old':('service.py','r[k] or old[k]','r[k]'),
        'wrong_revision':('service.py',"revision!=out['tickets'][ident]['revision']",'False'),
        'allows_bool_revision':('service.py','type(revision) is not int','not isinstance(revision,int)'),
        'direct_save':('persistence.py','os.replace(name,path)',"path.write_text('{}'); raise OSError('lost original')"),
        'wrong_report':('report.py',"'total':len(state['tickets'])","'total':0"),
        'initial_unimplemented':'initial',
    }
    rows = []
    for name,mutation in versions.items():
        root = tmp_path/name/'workspace'
        shutil.copytree(package/('initial' if mutation=='initial' else 'reference'),root)
        if isinstance(mutation,tuple):
            filename,old,new = mutation
            path = root/'tickets'/filename
            source = path.read_text()
            assert old in source
            path.write_text(source.replace(old,new))
        result = await score(root,package/'grade.py',tmp_path/name/'grading')
        expected = name=='reference'
        assert result['passed'] is expected,(name,result)
        rows.append({'variant':name,'passed':result['passed'],'expected':expected})
    save_report(tmp_path/'grader-regression.json',{'model_calls':0,'rows':rows})
    assert snapshot(package)==before


async def test_pinned_codex_accepts_feedback_after_prior_turn_completed(tmp_path,monkeypatch):
    from goalkeeper.native_log import native_events
    directory = tmp_path/'run'
    workspace = directory/'workspace'
    workspace.mkdir(parents=True)
    task = Task(id='offline',objective='Complete the local fixture.',constraints=[],
        acceptance=['synthetic'],write_paths=['result.txt'],
        checks=[{'id':'ready','argv':['/bin/true'],'timeout':1}],
        feedback={'text':'Complete the additional local fixture.','acceptance':['synthetic additional'],
            'probe':{'id':'progress','argv':['/bin/true'],'timeout':2},'checks':[]})
    requests = []
    def upstream(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,stream=SlowResponse())
    meter = MeteredTransport(tmp_path/'auth',tmp_path/'usage.json',auth=Auth(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    await meter.start()
    from types import SimpleNamespace
    route = meter.register(SimpleNamespace(id='codex',deadline=time.time()+15,
                                          config=SimpleNamespace(engine='codex')))
    errors = []
    class Consumer(CodexClient):
        def use_subscription_transport(self,*args):
            super().use_subscription_transport(*args)
            self.transport_overrides.update(HTTP_PROXY='',HTTPS_PROXY='',ALL_PROXY='',
                                            NO_PROXY='127.0.0.1,localhost')
        async def set_goal(self,*args):
            await self.events.put({'method':'thread/goal/updated','params':{
                'threadId':self.thread_id,'goal':{'status':'complete'}}})
        async def steer(self,*args):
            try:
                return await super().steer(*args)
            except Exception as error:
                errors.append(str(error))
                raise
    monkeypatch.setattr('goalkeeper.native.CodexClient',Consumer)
    adapter = Native('codex',directory,task,workspace,route['DIAGNOSTIC_TRANSPORT_BASE'],
                     route['LOCAL_ROUTE_TOKEN'],seconds=15)
    async def slow_probe(*args):
        await asyncio.sleep(1)  # The real first native turn completes during this probe.
        return {'exit_code':0,'timed_out':False}
    adapter.runner.run = slow_probe
    try:
        assert await adapter.run(),(directory/'native-result.json').read_text()
        assert errors and len(requests) == 2 and meter.known() == 34
        assert adapter.feedback_delivered
        assert any(e.get('type')=='common_feedback_delivered' for e in native_events(directory))
        # The new prompt is delivered through Codex, not an external planner.
        assert '用户追加要求：' in json.dumps(requests[-1],ensure_ascii=False)
    finally:
        await meter.close()
