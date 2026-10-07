"""Offline public/private contract and summary failure lifecycle regressions."""
import asyncio
import importlib.util
import json
import shutil
import time

import pytest

from goalkeeper.context import build, summary_view, validate_summary
from goalkeeper.engine import Engine, SummaryLimit
from goalkeeper.models import ModelEvent, RunConfig, Task
from goalkeeper.paths import PROJECT
from goalkeeper.sandbox import Seatbelt, Scope, runtime_roots
from goalkeeper.storage import canonical, fingerprint, snapshot
from test_context_provider import summary
from test_loop import call, fixture


def script(name):
    spec = importlib.util.spec_from_file_location('contract_' + name, PROJECT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compact_config(**overrides):
    return RunConfig(**dict(compact_trigger=3000, context_ceiling=12000,
        keep_recent_tokens=500, summary_max_tokens=400, summary_input_tokens=10000,
        **overrides))


class SummaryProvider:
    def __init__(self, generate):
        self.generate, self.requests = generate, []

    async def stream(self, request):
        self.requests.append(request)
        yield ModelEvent(type='response.completed', data={'response':{
            'model':request.config.model, 'usage':{'input_tokens':100,'output_tokens':50},
            'output':[{'type':'message','content':[{'type':'output_text',
                'text':canonical(self.generate(request))}]}]}})


@pytest.mark.parametrize('field', ['constraints', 'completed', 'decisions', 'unresolved', 'next_steps', 'source_refs'])
def test_summary_rejects_object_array_before_hashing(tokens, field):
    value = summary()
    value[field] = [{'ref':'invalid-object'}]
    with pytest.raises(ValueError, match='字段类型'):
        validate_summary(value, tokens, 2000)


async def test_summary_real_tool_refs_and_encrypted_archive(tmp_path, tokens):
    engine,_ = fixture(tmp_path,tokens,[],config=compact_config())
    child = engine.store.archive({'stdout':'verified local tool evidence'})
    opaque = {'type':'reasoning','id':'r','encrypted_content':'OPAQUE_SECRET'*1000}
    old = [opaque, call('read', {'path':'result.txt'}, 'source'),
        {'type':'function_call_output','call_id':'source','output':canonical({'stdout_reference':child})},
        {'role':'user','content':'recent ' * 600}]
    engine.state.history = old
    def generate(request):
        payload=json.loads(request.input[0]['content'])
        assert child in payload['allowed_source_refs']
        assert payload['output_schema']['properties']['source_refs']['items']['enum']==payload['allowed_source_refs']
        assert 'OPAQUE_SECRET' not in request.input[0]['content']
        value=summary()
        value['source_refs']=[child]
        return value
    engine.provider=SummaryProvider(generate)
    assert build(engine.state,tokens,[]).input[1]['encrypted_content']==opaque['encrypted_content']
    assert 'encrypted_content' not in summary_view(old)[0]
    await engine.compact()
    assert engine.state.summary and child in engine.state.summary['source_refs']
    assert len(engine.provider.requests)==1
    parent=json.loads(engine.provider.requests[0].input[0]['content'])['source']
    assert json.loads(engine.store.read(parent))[0]['encrypted_content']==opaque['encrypted_content']
    assert old[0]['encrypted_content']==opaque['encrypted_content']


async def test_summary_failure_breaker_survives_changed_history_and_resume(tmp_path,tokens):
    engine,_=fixture(tmp_path,tokens,[],config=compact_config())
    engine.state.history=[{'role':'user','content':'old '*2500},{'role':'user','content':'recent '*500}]
    original=json.loads(canonical(engine.state.history))
    engine.provider=SummaryProvider(lambda request:{})
    await engine.compact()
    assert engine.state.compaction_disabled=='summary_invalid' and len(engine.provider.requests)==2
    assert engine.state.summary is None and engine.state.history==original
    engine.state.history.append({'role':'user','content':'New tool evidence and feedback'})
    engine.state.goal_version+=1
    engine.save('synthetic_changed_history')
    await engine.compact()
    restored=Engine(engine.store.load(),engine.store,engine.provider,tokens)
    await restored.compact()
    assert len(engine.provider.requests)==2 and restored.state.compaction_disabled=='summary_invalid'


async def test_summary_fabricated_source_is_rejected_without_losing_history(tmp_path,tokens):
    engine,_=fixture(tmp_path,tokens,[],config=compact_config())
    engine.state.history=[{'role':'user','content':'old '*2500},{'role':'user','content':'recent '*500}]
    original=canonical(engine.state.history)
    unrelated=engine.store.archive({'unrelated':'not present in supplied evidence'})
    def generate(request):
        value=summary()
        value['source_refs']=[unrelated]
        return value
    engine.provider=SummaryProvider(generate)
    await engine.compact()
    assert engine.state.compaction_disabled=='summary_invalid' and len(engine.provider.requests)==2
    assert canonical(engine.state.history)==original


@pytest.mark.parametrize('kind',['tokens','time','input'])
async def test_summary_limits_before_send_preserve_main_budget(tmp_path,tokens,kind):
    engine,_=fixture(tmp_path,tokens,[],config=compact_config())
    request,_,_=engine.summary_request([{'role':'user','content':'bounded'}],None)
    if kind=='tokens':
        engine.state.calls=[{'role':'summary','forecast':99999,'usage':{'input_tokens':99999,'output_tokens':0},'started_at':time.time(),'finished_at':time.time()}]
    elif kind=='time':
        engine.state.calls=[{'role':'summary','forecast':0,'usage':{'input_tokens':0,'output_tokens':0},'started_at':time.time()-400,'finished_at':time.time()}]
    else:
        request.input[0]['content']='oversize '*15000
    before=len(engine.state.calls)
    with pytest.raises(SummaryLimit):
        await engine.call(request,'summary')
    assert len(engine.state.calls)==before and not engine.provider.requests


async def test_summary_timeout_disables_only_compaction(tmp_path,tokens):
    engine,_=fixture(tmp_path,tokens,[],config=compact_config(summary_time_fraction=.001))
    engine.state.config.seconds_budget=20
    engine.state.history=[{'role':'user','content':'old '*2500},{'role':'user','content':'recent '*500}]
    class Pending:
        async def stream(self, request):
            await asyncio.Event().wait()
            yield
    engine.provider=Pending()
    await engine.compact()
    assert engine.state.compaction_disabled=='summary_time_budget'
    assert len(engine.state.calls)==1 and engine.state.calls[0]['status']=='failed'
    assert 'usage' not in engine.state.calls[0] and len(engine.state.history)==2


async def test_c1_v2_public_private_contract_and_critical_mutations(tmp_path):
    package=PROJECT/'evaluation/private/C1-v2'
    before=snapshot(package)
    generated=tmp_path/'generated'
    assert script('prepare_candidate').prepare(generated)==generated
    assert snapshot(generated)==before
    assert fingerprint(snapshot(PROJECT/'evaluation/private/C1'))=='4a71b70c1170d50e2809672400513c3190b6ff6d77e22dd692d0ed02b9184911'
    rows=await script('validate_candidate').validate(package,tmp_path/'validation')
    assert len(rows)==13 and all(row['correct'] for row in rows)
    task=Task.model_validate_json((package/'task.json').read_text())
    # All four published checks use the same file-path contract as private grading.
    for variant in ('reference','inline_cli_payload','replay_reads_file','replay_rewrites_database'):
        root=tmp_path/'validation/workspaces'/variant
        for path in (package/'initial').glob('public*.py'):
            shutil.copyfile(path,root/path.name)
        runner=Seatbelt(Scope(root, runtimes=runtime_roots(), write_paths=task.write_paths, protected_files=[root/p for p in task.protected_paths]), tmp_path/'public-results'/variant)
        results=[]
        for check in task.checks+task.feedback.checks:
            result=await runner.run(check.argv,check.timeout)
            results.append(result['exit_code']==0 and not result['timed_out'])
        assert all(results) is (variant=='reference'),(variant,results)
    assert snapshot(package)==before


async def test_summary_failed_correction_still_allows_verified_delivery(tmp_path,tokens):
    from goalkeeper.evaluation import score
    package=PROJECT/'evaluation/private/C1-v2'
    task=Task.model_validate_json((package/'task.json').read_text())
    engine,_=fixture(tmp_path,tokens,[],task=task,config=compact_config())
    shutil.copytree(package/'initial',engine.root,dirs_exist_ok=True)
    engine.tools.runner=Seatbelt(engine.tools.scope, tmp_path/'commands')
    engine.state.history=[{'role':'user','content':'old synthetic evidence '*1500},
                          {'role':'user','content':'current '*600}]
    writes=[call('write',{'path':str(p.relative_to(package/'reference')),'content':p.read_text()},str(index))
            for index,p in enumerate(sorted((package/'reference/tickets').glob('*.py')))]
    main_requests=[]
    class Lifecycle:
        summary_count=0
        async def stream(self,request):
            if not request.tools:
                self.summary_count+=1
                output=[{'type':'message','content':[{'type':'output_text','text':'{}'}]}]
            else:
                main_requests.append(request)
                output=writes if len(main_requests)==1 else [call('request_delivery',{
                    'summary':'synthetic reference installed for offline verification','check_ids':[], 'unresolved':[]},'delivery')]
            yield ModelEvent(type='response.completed',data={'response':{'model':request.config.model,
                'usage':{'input_tokens':100,'output_tokens':50},'output':output}})
    engine.provider=Lifecycle()
    state=await engine.run()
    assert state.phase=='delivered' and state.feedback_delivered
    assert engine.provider.summary_count==2 and len(main_requests)==2
    assert state.compaction_disabled=='summary_invalid'
    result=await score(engine.store.directory/'delivery/snapshot',package/'grade.py',tmp_path/'independent-score')
    assert result['passed'] and len(result['checks'])==8
    assert state.delivery['goal_version']==2 and engine.store.load().phase=='delivered'


def test_offline_gate_rejects_changed_task_package(tmp_path,monkeypatch):
    from goalkeeper.readiness import REQUIRED_TESTS,require_offline_readiness,package_identity
    from goalkeeper.storage import save_report
    monkeypatch.setattr('goalkeeper.readiness.test_identity',lambda:'tests')
    files=[tmp_path/'tests.xml',tmp_path/'ruff.txt']
    for path in files:
        path.write_text('passed')
    proof=tmp_path/'proof.json'
    identity={'source_hash':'synthetic'}
    save_report(proof,{'kind':'offline_readiness','passed':True,'tests_hash':'tests',
        'execution_identities':[identity],'packages':package_identity(),
        'verified_cases':list(REQUIRED_TESTS),'evidence':[{'path':str(p),'hash':fingerprint(p.read_text())} for p in files]})
    require_offline_readiness(identity,proof,PROJECT/'evaluation/private/C1-v2')
    with pytest.raises(ValueError,match='完整离线验收'):
        require_offline_readiness(identity,proof,PROJECT/'evaluation/private/C1')
    monkeypatch.setattr('goalkeeper.readiness.package_identity',lambda:{'C1-v2':'tampered'})
    with pytest.raises(ValueError,match='完整离线验收'):
        require_offline_readiness(identity,proof,PROJECT/'evaluation/private/C1-v2')
    with pytest.raises(ValueError,match='完整离线验收'):
        require_offline_readiness(identity,proof,PROJECT/'evaluation/private/C1')
