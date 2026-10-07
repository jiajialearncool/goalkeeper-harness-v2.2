"""Unlimited accounting and fresh linked-work task contract regressions."""
import asyncio
import copy
import importlib.util
import json
import shutil
import time

import httpx
import pytest

from goalkeeper.codex import CodexClient
from goalkeeper.engine import Stop
from goalkeeper.evaluation import score
from goalkeeper.models import RunConfig,Task
from goalkeeper.paths import PROJECT
from goalkeeper.sandbox import Seatbelt,Scope,runtime_roots
from goalkeeper.storage import snapshot,save_report
from test_contract_summary import SummaryProvider
from test_context_provider import summary
from test_loop import fixture,call
from test_transport_reliability import meter,route,Wire,body


def preparation():
    spec=importlib.util.spec_from_file_location('prepare_L1',PROJECT/'scripts/prepare_longchain.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_unlimited_forwarding_records_usage_without_token_stop(tmp_path):
    terminal={'type':'response.completed','response':{'model':'gpt-6.1-sol','output':[],
        'usage':{'input_tokens':8000000,'output_tokens':4000000}}}
    m=meter(tmp_path,lambda r:httpx.Response(200,stream=Wire(terminal=terminal)),
            per_run_limit=None,total_limit=None)
    try:
        for group in ('goalkeeper','codex','opencode','pi'):
            request=route(m,group,time.time()+10)
            assert b'response.completed' in await body(await m.response(group,request))
            assert m.admit(group)
        assert m.known()==48000000 and m.reserved()==0 and not m.stop.is_set()
        proof=json.loads(m.report.read_text())
        assert proof['per_run_stop_threshold'] is proof['total_stop_threshold'] is None
        assert proof['known_tokens']==48000000 and proof['unknown_calls']==0
        m.deadlines['goalkeeper']=time.time()-1
        record,problem=await m.reserve_call('goalkeeper',await request.json())
        assert record is None and b'diagnostic_time_budget' in problem.body
    finally:
        await m.close()


async def test_unlimited_summary_uses_task_time_without_cost_cutoff(tmp_path,tokens):
    config=RunConfig(token_budget=None,summary_budget_fraction=None,summary_time_fraction=None)
    engine,_=fixture(tmp_path,tokens,[],config=config)
    engine.state.calls=[{'role':'summary','forecast':9000000,'usage':{'input_tokens':9000000,'output_tokens':0},
        'started_at':time.time()-1000,'finished_at':time.time()}]
    engine.provider=SummaryProvider(lambda r:summary())
    request,_,_=engine.summary_request([{'role':'user','content':'synthetic'}],None)
    await engine.call(request,'summary')
    assert len(engine.provider.requests)==1 and engine.state.calls[-1]['status']=='completed'
    engine.state.deadline=time.time()+1
    with pytest.raises(Stop,match='time_budget'):
        await engine.call(request,'summary')
    assert len(engine.provider.requests)==1


async def test_pinned_codex_goal_has_no_token_budget(tmp_path):
    root=tmp_path/'workspace'
    root.mkdir()
    client=CodexClient(root,runtime_roots(),write_paths=['result.txt'])
    home=tmp_path/'private-codex-home'
    home.mkdir()
    client.use_subscription_transport(home,'synthetic-offline')
    client.provider_settings['model_providers.openai_chatgpt_plan.base_url']='"http://127.0.0.1:1/v1"'
    try:
        await asyncio.wait_for(client.start(),20)
        await client.create_thread(root)
        first=await client.set_goal('Offline goal protocol check only',None)
        assert first['goal']['tokenBudget'] is None and first['goal']['tokensUsed']==0
        change=await client.set_goal('Offline changed objective protocol check only',None)
        assert change['goal']['tokenBudget'] is None
        await client.request('thread/goal/clear',{'threadId':client.thread_id})
    finally:
        await client.close()


async def test_longchain_reference_public_private_and_mutations(tmp_path):
    package=PROJECT/'evaluation/private/L1-v1'
    before=snapshot(package)
    generated=tmp_path/'generated'
    assert preparation().prepare(generated)==generated and snapshot(generated)==before
    mutations={
        'reference':None,
        'bool_revision':('domain.py','type(value) is not int','not isinstance(value,int)'),
        'duplicates_overwrite':('domain.py',"if item['id'] in out['tickets']:", 'if False:'),
        'no_state_conflict':('domain.py',"integer(plan['expected_revision'])!=original['revision']",'False'),
        'no_ticket_conflict':('domain.py',"integer(operation['expected_revision'],1)!=item['revision']",'False'),
        'replay_reexecutes':('domain.py',"if batch in original['batches']:",'if False:'),
        'wrong_stats':('domain.py','total=len(rows)','total=0'),
        'wrong_priority_order':('domain.py',"{'high':0,'normal':1,'low':2}","{'high':2,'normal':1,'low':0}"),
        'rollback_version_rewinds':('domain.py',"row['revision']=max(row['revision'],current['revision'])+1", "row['revision']=1"),
        'rollback_unrelated_rewinds':('domain.py',"for ident in frame['changed_ids']:","for ident in frame['before']:"),
        'inline_json_cli':('cli.py',"with open(path,encoding='utf-8') as f:return json.load(f)","return json.loads(path)"),
        'non_atomic_save':('repository.py','os.replace(name,path)',"path.write_text('{}');raise OSError('offline failure')"),
        'initial':'initial',
    }
    rows=[]
    for name,mutation in mutations.items():
        root=tmp_path/name/'workspace'
        shutil.copytree(package/('initial' if mutation=='initial' else 'reference'),root)
        if isinstance(mutation,tuple):
            filename,old,new=mutation
            file=root/'board'/filename
            source=file.read_text()
            assert old in source
            file.write_text(source.replace(old,new))
        verdict=await score(root,package/'grade.py',tmp_path/name/'grading')
        assert verdict['passed'] is (name=='reference'),(name,verdict)
        if name=='reference':
            assert len(verdict['checks'])==18
            for f in (package/'initial').glob('public*.py'):
                shutil.copyfile(f,root/f.name)
            runner=Seatbelt(Scope(root,runtimes=runtime_roots()))
            task=Task.model_validate_json((package/'task.json').read_text())
            for check in task.checks+[task.feedback.probe]+task.feedback.checks:
                output=await runner.run(check.argv,check.timeout)
                assert output['exit_code']==0 and not output['timed_out'],output
        rows.append({'variant':name,'passed':verdict['passed'],'expected':name=='reference','checks':verdict.get('checks',[])})
    save_report(tmp_path/'longchain-counterexamples.json',{'model_calls':0,'rows':rows,'all_correct':True})
    assert snapshot(package)==before


async def test_longchain_feedback_delivery_and_quality(tmp_path,tokens):
    package=PROJECT/'evaluation/private/L1-v1'
    task=Task.model_validate_json((package/'task.json').read_text())
    engine,_=fixture(tmp_path,tokens,[],task=task,config=RunConfig(token_budget=None,
        summary_budget_fraction=None,summary_time_fraction=None))
    shutil.copytree(package/'initial',engine.root,dirs_exist_ok=True)
    engine.tools.runner=Seatbelt(engine.tools.scope,tmp_path/'commands')
    files=[p for p in (package/'reference').rglob('*') if p.is_file()]
    writes=[call('write',{'path':str(p.relative_to(package/'reference')),'content':p.read_text()},str(i)) for i,p in enumerate(files)]
    from goalkeeper.provider import ScriptedProvider
    engine.provider=ScriptedProvider([writes,[call('request_delivery',{'summary':'offline reference fixture','check_ids':[],'unresolved':[]},'delivery')]])
    state=await engine.run()
    assert state.phase=='delivered' and state.feedback_delivered and state.goal_version==2
    verdict=await score(engine.store.directory/'delivery/snapshot',package/'grade.py',tmp_path/'grading')
    assert verdict['passed'] and len(verdict['checks'])==18
    assert state.config.token_budget is None and state.config.seconds_budget==1800
    assert engine.store.load().phase=='delivered'


def test_no_token_limit_is_explicit_and_not_large_number():
    value=json.loads((PROJECT/'configs/default.json').read_text())
    config=RunConfig.model_validate(value)
    assert config.token_budget is config.summary_budget_fraction is config.summary_time_fraction is None
    assert config.seconds_budget==1800
    config_copy=copy.deepcopy(config.model_dump())
    assert config_copy['token_budget'] is None
