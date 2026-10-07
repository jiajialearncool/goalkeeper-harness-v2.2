"""Synthetic full four-group evaluator; isolated state, no authentication or models."""
import asyncio
import json

import pytest

import goalkeeper.evaluation as evaluation
from goalkeeper.models import RunConfig, Task
from goalkeeper.storage import save_report


@pytest.fixture
def harness(tmp_path,monkeypatch):
    package = tmp_path/'package'
    (package/'initial').mkdir(parents=True)
    (package/'initial/public_check.py').write_text('synthetic protected check')
    (package/'grade.py').write_text('synthetic hidden scorer')
    task = Task(id='synthetic',objective='synthetic',constraints=[],acceptance=['READY'],
        write_paths=['result.txt'],protected_paths=['public_check.py'],
        checks=[{'id':'marker','argv':['/bin/true'],'timeout':.01}])
    (package/'task.json').write_text(task.model_dump_json())
    (tmp_path/'configs').mkdir()
    (tmp_path/'configs/default.json').write_text(RunConfig(seconds_budget=2,delivery_save_seconds=.01).model_dump_json())
    monkeypatch.setattr(evaluation,'PROJECT',tmp_path)
    monkeypatch.setattr('goalkeeper.cli.DATA',tmp_path/'runtime')
    monkeypatch.setattr(evaluation,'execution_identity',lambda *args:{'source_hash':'synthetic-fixed'})
    monkeypatch.setattr('goalkeeper.readiness.require_offline_readiness',lambda *args:None)
    options = {'native_normal':True,'stored_normal':None,'tamper':False,'core':False,'hang':False,'start_fail':False,
               'recovered_unknown':False}
    instances,cleaned,grading = [],set(),[]
    class Transport:
        def __init__(self,*args,**kwargs):
            self.stop = asyncio.Event()
            self.records = []
            self.first_reason = None
            self.retry_used = {}
            self.closed = False
            instances.append(self)
        async def start(self):
            if options['start_fail']:
                raise OSError('synthetic bind failure')
        def register(self,state):
            if options['recovered_unknown']:
                self.records.append({'run_id':state.id,'model':'gpt-6.1-sol','effort':'xhigh',
                    'status':'transport_error','usage_known':False,'tokens':0,
                    'request_id':state.id,'retry_scheduled':True})
            self.records.append({'run_id':state.id,'model':'gpt-6.1-sol','effort':'xhigh',
                'status':'completed','returned_model':'gpt-6.1-sol','usage_known':True,'tokens':17,
                'request_id':state.id,'forwarded':True})
            return {'LOCAL_ROUTE_TOKEN':'synthetic','DIAGNOSTIC_TRANSPORT_BASE':'http://127.0.0.1:1/v1'}
        def pause(self,reason):
            self.first_reason = self.first_reason or reason
            self.stop.set()
        async def wait_settled(self,*args):
            pass
        def known(self,run_id):
            return sum(c['tokens'] for c in self.records if c['run_id']==run_id)
        def reserved(self,*args):
            return 0
        async def close(self):
            self.closed = True
    class Engine:
        def __init__(self,state,store,*args):
            self.state,self.store = state,store
        async def run(self):
            try:
                if options['hang']:
                    await asyncio.Event().wait()
                if options['core']:
                    self.state.phase = 'program_error'
                    self.state.first_stop_reason = 'synthetic_core_error'
                    return
                self.state.phase = 'delivered'
                delivery = self.store.directory/'delivery/snapshot'
                delivery.mkdir(parents=True)
                (delivery/'result.txt').write_text('READY')
            finally:
                await asyncio.sleep(.01)
                cleaned.add('goalkeeper')
    class Native:
        def __init__(self,group,directory,task,workspace,*args,**kwargs):
            self.group,self.directory,self.root = group,directory,workspace
            self.feedback_delivered = False
            self.turn_index = 0
        async def run(self):
            try:
                if options['hang'] or options['core']:
                    await asyncio.Event().wait()
                (self.root/'result.txt').write_text('READY')
                if options['tamper'] and self.group == 'codex':
                    (self.root/'public_check.py').write_text('synthetic tamper')
                return options['native_normal']
            finally:
                await asyncio.sleep(.01)
                save_report(self.directory/'native-result.json',{'cleanup_complete':True,
                    'normal_end':options['native_normal'] if options['stored_normal'] is None else options['stored_normal'],
                    'feedback_delivered':False,'stop_reason':None})
                cleaned.add(self.group)
    async def score(*args):
        grading.append(args)
        return {'passed':True}  # Actual scorer parsing is tested separately.
    monkeypatch.setattr(evaluation,'MeteredTransport',Transport)
    monkeypatch.setattr(evaluation,'Engine',Engine)
    monkeypatch.setattr(evaluation,'Native',Native)
    monkeypatch.setattr(evaluation,'score',score)
    return package,options,instances,cleaned,grading


async def run(harness):
    return await evaluation.evaluate(harness[0],authorized=True,shared_limit=1000,connectivity=True)


async def test_full_evaluator_seals_four_passes_with_final_cost_and_timings(harness):
    directory = await run(harness)
    data = json.loads((directory/'results.json').read_text())
    # CONNECT tool proof is intentionally absent in this synthetic lifecycle;
    # require it rather than allowing the fake model/normal end to count.
    assert all(r['normal_delivery'] and r['artifact_passed'] and not r['passed'] for r in data['results'])
    assert all(r['cost']['known_tokens']==17 and r['cost']['usage_complete'] for r in data['results'])
    assert all(r['grading_seconds']>=0 and r['accounting_wait_seconds']>=0 for r in data['results'])
    assert harness[2][0].closed and harness[3] == set(evaluation.GROUPS)


async def test_artifacts_alone_never_mean_native_delivery(harness):
    harness[1]['native_normal'] = False
    directory = await run(harness)
    rows = json.loads((directory/'results.json').read_text())['results']
    assert all(r['artifact_passed'] and not r['normal_delivery'] and not r['passed'] for r in rows[1:])


async def test_full_evaluator_rejects_changed_protected_checks(harness):
    harness[1]['tamper'] = True
    directory = await run(harness)
    rows = json.loads((directory/'results.json').read_text())['results']
    assert rows[1]['protected_checks_unchanged'] is False and rows[1]['passed'] is False
    assert all(r['protected_checks_unchanged'] for r in [rows[0],*rows[2:]])


async def test_full_evaluator_outer_cancel_saves_report_after_all_cleanup(harness):
    harness[1]['hang'] = True
    running = asyncio.create_task(run(harness))
    while not harness[2] or len(harness[2][0].records)<4:
        await asyncio.sleep(0)
    await asyncio.sleep(.02)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    files = list((harness[0].parent/'runtime/evaluations').glob('*/results.json'))
    assert len(files)==1
    data = json.loads(files[0].read_text())
    assert data['cancelled'] and len(data['results']) == 4
    assert all(r['cost']['known_tokens']==17 and not r['passed'] for r in data['results'])
    assert harness[2][0].closed and harness[3] == set(evaluation.GROUPS)


async def test_core_failure_stops_peers_before_grading_and_preserves_results(harness):
    harness[1]['core'] = True
    directory = await asyncio.wait_for(run(harness),1)
    data = json.loads((directory/'results.json').read_text())
    assert data['core_program_error'] and all(not r['passed'] for r in data['results'])
    assert not harness[4]
    assert harness[2][0].closed and harness[3] == set(evaluation.GROUPS)
    assert data['results'][0]['stop_reason']=='synthetic_core_error'


async def test_transport_start_failure_closes_clients_without_starting_models(harness):
    harness[1]['start_fail'] = True
    with pytest.raises(OSError,match='bind'):
        await run(harness)
    assert harness[2][0].closed and not harness[2][0].records and not harness[3]
    files = list((harness[0].parent/'runtime/evaluations').glob('*/startup-error.json'))
    assert len(files)==1 and json.loads(files[0].read_text())['model_calls']==0


async def test_real_connect_rejects_missing_current_offline_proof_before_auth(tmp_path,monkeypatch):
    called = []
    monkeypatch.setattr(evaluation,'MeteredTransport',lambda *args,**kwargs:called.append('auth'))
    with pytest.raises(ValueError,match='完整离线验收'):
        await evaluation.evaluate(tmp_path,authorized=True,shared_limit=100,
            connectivity=True,offline_readiness=tmp_path/'nonexistent-proof.json')
    assert not called


async def test_repeated_outer_cancel_waits_for_four_cleanup_and_final_report(harness):
    harness[1]['hang'] = True
    running = asyncio.create_task(run(harness))
    while not harness[2] or len(harness[2][0].records)<4:
        await asyncio.sleep(0)
    await asyncio.sleep(.01)
    running.cancel()
    await asyncio.sleep(.001)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    files = list((harness[0].parent/'runtime/evaluations').glob('*/results.json'))
    assert len(files)==1 and json.loads(files[0].read_text())['cancelled']
    assert harness[3] == set(evaluation.GROUPS) and harness[2][0].closed


async def test_completed_native_delivery_is_not_erased_by_late_cleanup_cancel(harness):
    harness[1]['native_normal'] = False  # The outer wrapper can return cancellation.
    harness[1]['stored_normal'] = True  # Native Goal and turn had already completed.
    directory = await run(harness)
    rows = json.loads((directory/'results.json').read_text())['results']
    assert all(row['normal_delivery'] and row['cleanup_complete'] for row in rows[1:])


async def test_recovered_delivery_and_usage_completeness_are_independent(harness, monkeypatch):
    harness[1]['recovered_unknown'] = True
    monkeypatch.setattr(evaluation, 'connectivity_tools', lambda *args: {
        'completed_tools': 1, 'check_execution_verified': True})
    directory = await run(harness)
    rows = json.loads((directory/'results.json').read_text())['results']
    assert all(row['passed'] and row['model_verified'] and row['normal_delivery'] for row in rows)
    assert all(not row['cost']['usage_complete'] and not row['efficiency_comparable'] for row in rows)
    assert all(row['cost']['unknown_calls']==1 and row['cost']['known_tokens']==17 for row in rows)
