"""Offline fault injection for the complete execution/reporting lifecycle."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from goalkeeper.compat import subscription_payload
from goalkeeper.evaluation import execute_until_stopped, parallel, score, connectivity_tools
from goalkeeper.models import RunConfig
from goalkeeper.native_log import NativeLog, native_events
from goalkeeper.sandbox import PermissionDenied, Scope
from goalkeeper.storage import Store, save_report
from goalkeeper.auth import AuthError
from test_loop import fixture, call
from test_transport_reliability import meter, route, body, Wire, complete


async def test_progress_flood_yields_to_peers_and_writes_linear_control_evidence(tmp_path):
    log = NativeLog(tmp_path)
    ticks = 0
    done = asyncio.Event()
    async def peer():
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(peer())
    for i in range(20_000):
        await log.append({'type': 'message_update', 'assistantMessageEvent': {'text': 'x'*100_000}})
        if i % 1000 == 0:
            await log.append({'type': 'tool_execution_end', 'number': i})
    done.set()
    await task
    await log.close()
    summary = json.loads((tmp_path/'native-log-summary.json').read_text())
    assert ticks >= 20_000
    assert log.path.stat().st_size == summary['bytes_written'] < 2500
    assert summary['progress_events_omitted'] == 20_000
    assert [e['number'] for e in native_events(tmp_path)] == list(range(0,20_000,1000))
    assert not (tmp_path/'native-events.json').exists()


async def test_append_cancellation_waits_for_disk_write_before_next_event(tmp_path, monkeypatch):
    import threading
    log = NativeLog(tmp_path)
    started, finish = threading.Event(), threading.Event()
    append = log._append
    def delayed(value):
        started.set()
        finish.wait(2)
        append(value)
    monkeypatch.setattr(log, '_append', delayed)
    writer = asyncio.create_task(log.append({'type':'first'}))
    await asyncio.to_thread(started.wait, 1)
    writer.cancel()
    second = asyncio.create_task(log.append({'type':'second'}))
    await asyncio.sleep(.01)
    assert not second.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await writer
    await second
    assert [e['type'] for e in native_events(tmp_path)] == ['first','second']


async def test_connect_tool_evidence_reads_append_only_journal(tmp_path):
    from goalkeeper.paths import PROJECT
    log = NativeLog(tmp_path)
    await log.append({'type':'tool_execution_start','toolCallId':'a', 'args':{
        'command':str(PROJECT/'.venv/bin/python')+' public_check.py'}})
    await log.append({'type':'tool_execution_end','toolCallId':'a','toolName':'bash',
        'isError':False,'result':{'content':[{'type':'text','text':'CONNECT_CHECK_PASSED'}]}})
    assert connectivity_tools('pi', None, tmp_path) == {
        'completed_tools':1,'check_execution_verified':True}


async def test_outer_cancel_waits_for_all_four_job_cleanup():
    started = asyncio.Event()
    cleaned = set()
    count = 0
    async def job(group):
        nonlocal count
        count += 1
        if count == 4:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(.01)
            cleaned.add(group)
    running = asyncio.create_task(parallel([lambda g=g:job(g) for g in range(4)], asyncio.Event()))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cleaned == set(range(4))


async def test_shared_stop_settles_accepted_work_then_bounds_native_backoff():
    transport = SimpleNamespace(stop=asyncio.Event(),first_reason='shared_service')
    settled = asyncio.Event()
    async def wait_settled(*args):
        await settled.wait()
    transport.wait_settled = wait_settled
    reason = []
    async def operation():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as e:
            reason.extend(e.args)
            raise
    run = asyncio.create_task(execute_until_stopped(operation(),transport,'one',time.time()+5,.01))
    transport.stop.set()
    await asyncio.sleep(.02)
    assert not run.done()
    settled.set()
    assert await asyncio.wait_for(run,.2) is False
    assert reason == ['cohort_shared_service']


async def test_already_finished_delivery_survives_shared_stop():
    transport = SimpleNamespace(stop=asyncio.Event(),first_reason='shared_service')
    async def wait(*args):
        await asyncio.Event().wait()
    transport.wait_settled = wait
    async def done():
        return 'delivered'
    transport.stop.set()
    assert await execute_until_stopped(done(),transport,'one',time.time()+1,.01) == 'delivered'


@pytest.mark.parametrize('args', [None, [], {'path':7,'content':'x'},
    {'path':'result.txt','content':False}, {'argv':['echo',7]},
    {'argv':['true'],'timeout':float('nan')}, {'argv':['true'],'timeout':True}])
async def test_model_argument_errors_are_tool_failures_not_core_failure(tmp_path,tokens,args):
    name = 'bash' if isinstance(args,dict) and 'argv' in args else 'write'
    engine,_ = fixture(tmp_path,tokens,[[call(name,args,1)],
        [call('write',{'path':'result.txt','content':'READY'},2)],
        [call('request_delivery',{'summary':'done','check_ids':[],'unresolved':[]},3)]])
    state = await engine.run()
    assert state.phase == 'delivered'
    assert state.operations[0]['status'] == 'failed'
    assert state.ended_at is not None and engine.store.load().ended_at == state.ended_at


async def test_copy_failure_cannot_publish_or_claim_delivery(tmp_path,tokens,monkeypatch):
    engine,_ = fixture(tmp_path,tokens,[[call('write',{'path':'result.txt','content':'READY'},1)],
        [call('request_delivery',{'summary':'done','check_ids':[],'unresolved':[]},2)],[],[]])
    def fail(*args):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr('goalkeeper.engine.shutil.copyfile',fail)
    state = await engine.run()
    assert state.phase != 'delivered' and state.delivery is None
    assert not (engine.store.directory/'delivery/snapshot').exists()
    assert state.operations[1]['status'] == 'failed'


async def test_duplicate_tool_id_in_old_operation_is_protocol_error(tmp_path,tokens):
    engine,_ = fixture(tmp_path,tokens,[[call('write',{'path':'result.txt','content':'NEW'},'used')]])
    engine.state.operations.append({'call_id':'used','status':'completed','name':'write'})
    state = await engine.run()
    assert state.phase == 'upstream_error'
    assert not (engine.root/'result.txt').exists()


async def test_invalid_resume_keeps_valid_checkpoint_and_attempt_count(tmp_path,tokens):
    engine,_ = fixture(tmp_path,tokens,[])
    engine.state.phase = 'resource_interrupted'
    engine.state.recovery_used = 1
    engine.store.save(engine.state,'synthetic_resume_used')
    before = engine.store.path.read_bytes()
    with pytest.raises(ValueError):
        await engine.run(resume=True)
    assert engine.store.path.read_bytes() == before
    assert not engine.provider.requests


async def test_cleanup_failure_preserves_closed_checkpoint_and_releases_lease(tmp_path,tokens):
    engine,_ = fixture(tmp_path,tokens,[[],[]])
    async def failed_stop():
        raise OSError('synthetic cleanup')
    engine.tools.runner.stop = failed_stop
    state = await engine.run()
    assert state.phase == 'program_error' and state.ended_at is not None
    assert engine.store.load().phase == 'program_error'
    assert (engine.store.directory/'cleanup-error.json').exists()
    with engine.store.lease():
        pass


@pytest.mark.parametrize('value', [[], {'passed':'false'}, {'passed':1}, {'passed':True}])
async def test_grader_requires_json_object_and_literal_true(tmp_path,monkeypatch,value):
    root = tmp_path/'workspace'
    root.mkdir()
    output = tmp_path/'grader-output'
    output.write_text(json.dumps(value))
    class FakeRunner:
        def __init__(self,*args,**kwargs):
            pass
        async def run(self,*args):
            return {'stdout_file':str(output),'exit_code':0,'timed_out':False}
    monkeypatch.setattr('goalkeeper.evaluation.Seatbelt',FakeRunner)
    result = await score(root,tmp_path/'grade.py',tmp_path/'result')
    assert result['passed'] is (isinstance(value,dict) and value.get('passed') is True)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_budget_and_timeout_rejected(value):
    with pytest.raises(ValidationError):
        RunConfig(seconds_budget=value)


@pytest.mark.parametrize('payload', [[], None, {'reasoning':None},
    {'model':'gpt-6.1-sol','reasoning':{'effort':'xhigh'},'input':[None]},
    {'model':'gpt-6.1-sol','reasoning':{'effort':'xhigh'},'input':[],'include':[3]}])
def test_invalid_native_wire_types_are_configuration_error(payload):
    with pytest.raises(ValueError):
        subscription_payload(payload)


def test_corrupt_archive_cannot_be_reused(tmp_path):
    store = Store(tmp_path)
    ref = store.archive('synthetic original')
    (store.archives/ref).write_text('corrupt')
    with pytest.raises(ValueError,match='损坏'):
        store.archive('synthetic original')


def test_failed_atomic_save_retains_previous_and_removes_temporary(tmp_path,monkeypatch):
    path = tmp_path/'report.json'
    save_report(path,{'sequence':1})
    def fail(*args):
        raise OSError('synthetic replace')
    monkeypatch.setattr('goalkeeper.storage.os.replace',fail)
    with pytest.raises(OSError):
        save_report(path,{'sequence':2})
    assert json.loads(path.read_text()) == {'sequence':1}
    assert not list(tmp_path.glob('*.tmp'))


def test_scope_denies_protected_file_inside_writable_directory(tmp_path):
    scope = Scope(tmp_path,write_paths=['tickets/'],protected_files=[tmp_path/'tickets/check.py'])
    with pytest.raises(PermissionDenied):
        scope.path('tickets/check.py',write=True)
    assert scope.path('tickets/solution.py',write=True) == tmp_path/'tickets/solution.py'


async def test_authentication_failure_before_send_is_known_zero_and_not_retried(tmp_path):
    sent = []
    m = meter(tmp_path,lambda request:sent.append(request) or httpx.Response(200),retries=1)
    async def fail():
        raise AuthError('synthetic missing credentials')
    m.auth.access_token = fail
    response = await m.response('one',route(m))
    assert response.status_code == 401 and not sent
    assert m.records[0]['status'] == 'not_sent' and m.records[0]['usage_known']
    assert m.known() == m.reserved() == 0 and not m.retry_used
    assert m.first_reason == 'configuration_error'
    await m.close()


async def test_quota_terminal_is_distinct_from_retryable_service_break(tmp_path):
    terminal = {'type':'response.failed','response':{'model':'gpt-6.1-sol',
        'error':{'code':'quota_exceeded'},'usage':{'input_tokens':9,'output_tokens':2}}}
    m = meter(tmp_path,lambda request:httpx.Response(200,stream=Wire(terminal=terminal)),retries=1)
    output = await body(await m.response('one',route(m)))
    assert m.first_reason == 'quota_unavailable' and b'quota_exceeded' in output
    assert len(m.records) == 1 and not m.retry_used and m.known() == 11
    await m.close()


async def test_completed_event_with_incomplete_status_never_publishes_tools(tmp_path):
    terminal = complete()
    terminal['response']['status'] = 'incomplete'
    m = meter(tmp_path,lambda request:httpx.Response(200,stream=Wire(terminal=terminal)))
    output = await body(await m.response('one',route(m)))
    assert b'discard-me' not in output and m.records[0]['status'] == 'terminal_error'
    assert m.known() == 17
    await m.close()


async def test_codex_feedback_after_ended_turn_starts_native_continuation():
    from goalkeeper.codex import CodexClient, ProtocolError
    client = CodexClient()
    client.thread_id = 'root'
    calls = []
    async def steer(text,turn):
        calls.append(('steer',turn))
        raise ProtocolError('synthetic no active turn',{'code':-32602})
    async def read(method,params):
        assert method == 'thread/read'
        return {'thread':{'turns':[{'id':'old','status':'completed'}]}}
    async def start(text,*args):
        calls.append(('prompt',text))
        return {'turn':{'id':'new'}}
    client.steer,client.request,client.turn = steer,read,start
    assert await client.steer_or_start('用户追加要求：synthetic','old') == 'new'
    assert calls == [('steer','old'),('prompt','用户追加要求：synthetic')]


async def test_lost_codex_feedback_acknowledgement_is_never_replayed():
    from goalkeeper.codex import CodexClient, ProtocolError
    client = CodexClient()
    async def lost(*args):
        raise ProtocolError('synthetic connection closed')
    client.steer = lost
    with pytest.raises(ProtocolError,match='closed'):
        await client.steer_or_start('synthetic','old')
    assert not client.pending


async def test_pi_feedback_uses_rpc_receipt_and_waits_for_new_cycle(tmp_path,monkeypatch):
    from goalkeeper.native import Native
    from goalkeeper.models import Task
    root = tmp_path/'workspace'
    root.mkdir()
    task = Task(id='synthetic',objective='synthetic',constraints=[],acceptance=['synthetic'],
        write_paths=['result.txt'],checks=[{'id':'initial','argv':['/bin/true']}],
        feedback={'text':'additional','acceptance':['synthetic new'],
            'probe':{'id':'probe','argv':['/bin/true']},'checks':[]})
    adapter = Native('pi',tmp_path,task,root,'http://127.0.0.1:1234/v1','synthetic')
    reader = asyncio.StreamReader()
    events = [
        {'type':'tool_execution_end','toolCallId':'a'},
        {'type':'agent_end','messages':[{'role':'user','content':'initial'}]},
        {'type':'response','id':'common-feedback','success':True},
        {'type':'agent_end','messages':[{'role':'user','content':'用户追加要求：additional'},
            {'role':'assistant','stopReason':'stop','content':[]}]},
    ]
    for value in events:
        reader.feed_data((json.dumps(value)+'\n').encode())
    reader.feed_eof()
    stderr = asyncio.StreamReader()
    stderr.feed_eof()
    sent = []
    class Input:
        def write(self,raw):
            sent.append(json.loads(raw))
        async def drain(self):
            pass
    async def spawn(*args):
        adapter.process = SimpleNamespace(stdout=reader,stderr=stderr,stdin=Input(),returncode=0)
    async def check(*args):
        return {'exit_code':0,'timed_out':False}
    adapter.spawn,adapter.runner.run = spawn,check
    monkeypatch.setattr('goalkeeper.native.configuration',lambda *args:([],{}))
    assert await adapter.pi()
    assert adapter.feedback_delivered and adapter.feedback_queued
    assert sent[1] == {'type':'prompt','message':'用户追加要求：additional',
                       'id':'common-feedback','streamingBehavior':'steer'}
    evidence = list(native_events(tmp_path))
    assert sum(e.get('type')=='common_feedback_delivered' for e in evidence) == 1
    await adapter.stderr


async def test_native_feedback_not_sent_for_project_changed_during_probe(tmp_path):
    from goalkeeper.native import Native
    from goalkeeper.models import Task
    root = tmp_path/'workspace'
    root.mkdir()
    task = Task(id='synthetic',objective='synthetic',constraints=[],acceptance=['synthetic'],
        write_paths=['result.txt'],checks=[{'id':'initial','argv':['/bin/true']}],
        feedback={'text':'additional','acceptance':['synthetic'],
            'probe':{'id':'probe','argv':['/bin/true']},'checks':[]})
    adapter = Native('pi',tmp_path,task,root,'http://127.0.0.1:1234/v1','synthetic')
    async def probe(*args):
        (root/'result.txt').write_text('changed while checking')
        return {'exit_code':0,'timed_out':False}
    sent = []
    async def send(text):
        sent.append(text)
    adapter.runner.run = probe
    assert await adapter.feedback(send) is False
    assert not sent and not adapter.feedback_delivered


async def test_cancel_during_authentication_never_charges_unknown_model_tokens(tmp_path):
    sent,auth_started = [],asyncio.Event()
    m = meter(tmp_path,lambda request:sent.append(request) or httpx.Response(200))
    async def pending():
        auth_started.set()
        await asyncio.Event().wait()
    m.auth.access_token = pending
    operation = asyncio.create_task(m.response('one',route(m)))
    await auth_started.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert not sent and m.records[0]['status']=='not_sent'
    assert m.records[0]['usage_known'] and m.known()==m.reserved()==0
    await m.close()


@pytest.mark.parametrize('output', [[{'type':'message','content':None}],
    [{'type':'message','content':[None]}], [{'type':'message','content':[{'text':3}]}],None])
async def test_malformed_model_messages_do_not_become_cohort_program_error(tmp_path,tokens,output):
    engine,_ = fixture(tmp_path,tokens,[output])
    assert (await engine.run()).phase=='upstream_error'
    assert not engine.state.delivery


@pytest.mark.parametrize('argv', [[''],['true','\x00']])
def test_invalid_prescribed_command_rejected_before_any_run(argv):
    from goalkeeper.models import Check
    with pytest.raises(ValidationError):
        Check(id='bad',argv=argv)


@pytest.mark.parametrize('path', ['', '.', '../escape', '.git/config', '.home/private','x\x00'])
def test_invalid_task_scope_rejected_before_any_run(path):
    from goalkeeper.models import Task
    with pytest.raises(ValidationError):
        Task(id='bad',objective='synthetic',constraints=[],acceptance=['synthetic'],
             write_paths=[path],checks=[{'id':'ready','argv':['/bin/true']}])


async def test_standalone_completed_event_with_incomplete_status_is_not_executed(tmp_path,tokens):
    from goalkeeper.models import ModelEvent
    engine,_ = fixture(tmp_path,tokens,[])
    class Provider:
        async def stream(self,request):
            yield ModelEvent(type='response.completed',data={'response':{
                'status':'incomplete','model':'gpt-6.1-sol','usage':{'input_tokens':3,'output_tokens':2},
                'output':[call('write',{'path':'result.txt','content':'WRONG'},1)]}})
    engine.provider = Provider()
    state = await engine.run()
    assert state.phase=='upstream_error' and not (engine.root/'result.txt').exists()
    assert state.calls[0]['usage']=={'input_tokens':3,'output_tokens':2}


@pytest.mark.parametrize('passed',[True,False])
async def test_real_entry_exit_status_matches_final_verdict(tmp_path,monkeypatch,passed):
    import importlib.util
    import sys
    from goalkeeper.paths import PROJECT
    spec = importlib.util.spec_from_file_location('audit_real_entry',PROJECT/'scripts/run_real.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path/'results.json').write_text(json.dumps({'results':[{'passed':passed}]}))
    async def synthetic_evaluate(*args,**kwargs):
        return tmp_path
    monkeypatch.setattr(module,'evaluate',synthetic_evaluate)
    monkeypatch.setattr(sys,'argv',['run_real.py','--mode','candidate','--allow-real-model',
                                  '--authorized-token-limit','100'])
    if passed:
        await module.main()
    else:
        with pytest.raises(SystemExit) as error:
            await module.main()
        assert error.value.code==1


def test_offline_gate_accepts_only_current_untampered_evidence(tmp_path,monkeypatch):
    from goalkeeper.readiness import REQUIRED_TESTS, require_offline_readiness, package_identity
    from goalkeeper.storage import fingerprint
    monkeypatch.setattr('goalkeeper.readiness.test_identity',lambda:'synthetic-tests')
    files = [tmp_path/'tests.xml',tmp_path/'ruff.txt']
    for path in files:
        path.write_text('synthetic passing evidence')
    identity = {'source_hash':'synthetic-current'}
    proof = tmp_path/'proof.json'
    save_report(proof,{'kind':'offline_readiness','passed':True,
        'execution_identities':[identity], 'tests_hash':'synthetic-tests',
        'verified_cases':list(REQUIRED_TESTS),'packages':package_identity(),'evidence':[
            {'path':str(p),'hash':fingerprint(p.read_text())} for p in files]})
    assert require_offline_readiness(identity,proof)['passed']
    with pytest.raises(ValueError,match='完整离线验收'):
        require_offline_readiness({'source_hash':'changed'},proof)
    files[0].write_text('tampered')
    with pytest.raises(ValueError,match='完整离线验收'):
        require_offline_readiness(identity,proof)


@pytest.mark.parametrize('limit',[True,float('nan'),float('inf'),-1,0,'4000000'])
async def test_invalid_shared_threshold_never_creates_authenticated_transport(tmp_path,limit,monkeypatch):
    import goalkeeper.evaluation as evaluation
    created = []
    monkeypatch.setattr(evaluation,'MeteredTransport',lambda *args,**kwargs:created.append(True))
    with pytest.raises(ValueError,match='禁止真实评测'):
        await evaluation.evaluate(tmp_path,authorized=True,shared_limit=limit,connectivity=True)
    assert not created


@pytest.mark.parametrize('limit',[True,float('nan'),float('inf'),-1,0])
def test_transport_rejects_invalid_limits_before_creating_auth(tmp_path,limit,monkeypatch):
    from goalkeeper.transport import MeteredTransport
    created = []
    monkeypatch.setattr('goalkeeper.transport.AuthBroker',lambda *args:created.append(True))
    with pytest.raises(ValueError,match='正整数'):
        MeteredTransport(tmp_path/'auth',tmp_path/'cost.json',total_limit=limit)
    assert not created


async def test_native_repeated_cancel_finishes_cleanup_and_result(tmp_path):
    from goalkeeper.native import Native
    from goalkeeper.models import Task
    root = tmp_path/'workspace'
    root.mkdir()
    task = Task(id='synthetic',objective='synthetic',constraints=[],acceptance=['synthetic'],
        write_paths=['result.txt'],checks=[{'id':'ready','argv':['/bin/true']}])
    native = Native('pi',tmp_path,task,root,'http://127.0.0.1:1234/v1','synthetic')
    begun,closing,finished = asyncio.Event(),asyncio.Event(),asyncio.Event()
    async def pending():
        begun.set()
        await asyncio.Event().wait()
    async def cleanup():
        closing.set()
        await asyncio.sleep(.05)
        finished.set()
        return []
    native.pi,native.cleanup = pending,cleanup
    run = asyncio.create_task(native.run())
    await begun.wait()
    run.cancel('cohort_shared_service')
    await closing.wait()
    run.cancel('outer_cancel')
    await asyncio.sleep(.01)
    run.cancel('outer_cancel_again')
    with pytest.raises(asyncio.CancelledError):
        await run
    assert finished.is_set()
    result = json.loads((tmp_path/'native-result.json').read_text())
    assert result['stop_reason']=='cohort_shared_service' and result['cleanup_complete']
    assert (tmp_path/'native-log-summary.json').exists()


async def test_engine_repeated_cancel_closes_checkpoint_before_releasing_lease(tmp_path,tokens):
    from goalkeeper.models import ModelEvent
    engine,_ = fixture(tmp_path,tokens,[])
    begun,closing,finished = asyncio.Event(),asyncio.Event(),asyncio.Event()
    class Provider:
        async def stream(self,*args):
            begun.set()
            await asyncio.Event().wait()
            yield ModelEvent(type='unknown',data={})
    async def cleanup():
        closing.set()
        await asyncio.sleep(.05)
        finished.set()
    engine.provider = Provider()
    engine.tools.runner.stop = cleanup
    run = asyncio.create_task(engine.run())
    await begun.wait()
    run.cancel('cohort_shared_service')
    await closing.wait()
    run.cancel('outer_cancel')
    with pytest.raises(asyncio.CancelledError):
        await run
    assert finished.is_set()
    saved = engine.store.load()
    assert saved.ended_at is not None and saved.first_stop_reason=='cohort_shared_service'
    assert saved.events[-1]['kind']=='run_closed'
    with engine.store.lease():
        pass
