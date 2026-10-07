"""Fresh hard business chain and independent scoring/lifecycle regressions."""
import copy
import importlib.util
import json
import shutil

import pytest

from goalkeeper.evaluation import score
from goalkeeper.models import RunConfig,Task
from goalkeeper.paths import PROJECT
from goalkeeper.sandbox import Seatbelt,Scope,runtime_roots
from goalkeeper.storage import snapshot,save_report
from test_loop import fixture,call


def preparation():
    spec=importlib.util.spec_from_file_location('prepare_H1',PROJECT/'scripts/prepare_highchain.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_highchain_reference_public_private_and_mutations(tmp_path):
    package=PROJECT/'evaluation/private/H1-v1'
    before=snapshot(package)
    generated=tmp_path/'generated'
    assert preparation().prepare(generated)==generated and snapshot(generated)==before
    with pytest.raises(ValueError,match='已存在'):
        preparation().prepare(generated)
    mutations={
        'reference':None,
        'bool_integer':('core.py','type(value) is not int','not isinstance(value,int)'),
        'no_reservation_conservation':('core.py',"if any(state['lots'][key]['reserved']!=qty for key,qty in allocated.items()):",'if False:'),
        'wrong_fefo':('core.py',"key=lambda l:(l['expires_at'] is None,l['expires_at'] if l['expires_at'] is not None else 0,l['id'])","key=lambda l:l['id']"),
        'no_order_expiry':('core.py',"if order['expires_at']<=now:",'if False:'),
        'transfer_loses_expiry':('core.py',"new.update(id=action['new_lot_id'],warehouse=action['warehouse'],on_hand=action['quantity'],reserved=0)","new.update(id=action['new_lot_id'],warehouse=action['warehouse'],expires_at=None,on_hand=action['quantity'],reserved=0)"),
        'wrong_cancel':('core.py',"line['cancelled']=line['quantity']-line['shipped']","line['cancelled']=0"),
        'no_version_conflict':('core.py',"integer(envelope['expected_revision'])!=original['revision']",'False'),
        'no_early_replay':('core.py','if replay:return original,','if False:return original,'),
        'wrong_preview_delta':('core.py',"gross_delta=sum(e['amount'] for e in ledger if e['kind']=='sale')","gross_delta=0"),
        'wrong_refund_price':('core.py',"amount=item['quantity']*line['unit_price'],revision=revision,request_id=request))\n        # A return", "amount=0,revision=revision,request_id=request))\n        # A return"),
        'reopen_return':('core.py','line[\'returned\']+=item[\'quantity\']',"line['returned']+=item['quantity'];order['status']='open'"),
        'wrong_hash_encoding':('core.py',"sha256(canonical(body).encode('utf-8'))","sha256(canonical(body).encode('utf-8')+b'wrong')"),
        'no_projection_verify':('core.py','if data!=rebuild(data):','if False:'),
        'no_query_isolation':('core.py',"data=validate(state);fields(query,(),('status','warehouse','sku','offset','limit'))","data=state;fields(query,(),('status','warehouse','sku','offset','limit'))"),
        'inline_json_cli':('cli.py','envelope=read_json(args.input)','envelope=json.loads(args.input)'),
        'rebuild_validates_projection':('cli.py',"result=journal.rebuild(raw)","result=journal.rebuild(migration.migrate(raw))"),
        'non_atomic_save':('repository.py','os.replace(name,path)',"path.write_text('{}');raise OSError('offline failure')"),
        'initial':'initial',
    }
    rows=[]
    for name,mutation in mutations.items():
        root=tmp_path/name/'workspace'
        shutil.copytree(package/('initial' if mutation=='initial' else 'reference'),root)
        if isinstance(mutation,tuple):
            filename,old,new=mutation
            file=root/'supply'/filename
            source=file.read_text()
            assert old in source,(name,old)
            file.write_text(source.replace(old,new))
        verdict=await score(root,package/'grade.py',tmp_path/name/'grading')
        assert verdict['passed'] is (name=='reference'),(name,verdict)
        if name=='reference':
            assert len(verdict['checks'])==36
            for f in (package/'initial').glob('public*.py'):
                shutil.copyfile(f,root/f.name)
            runner=Seatbelt(Scope(root,runtimes=runtime_roots()))
            task=Task.model_validate_json((package/'task.json').read_text())
            for check in task.checks+[task.feedback.probe]+task.feedback.checks:
                output=await runner.run(check.argv,check.timeout)
                assert output['exit_code']==0 and not output['timed_out'],output
        rows.append({'variant':name,'passed':verdict['passed'],'expected':name=='reference','checks':verdict.get('checks',[])})
    save_report(tmp_path/'highchain-counterexamples.json',{'model_calls':0,'rows':rows,'all_correct':True})
    assert snapshot(package)==before


async def test_highchain_feedback_delivery_and_quality(tmp_path,tokens):
    package=PROJECT/'evaluation/private/H1-v1'
    task=Task.model_validate_json((package/'task.json').read_text())
    from goalkeeper.native import objective
    assert len(objective(task)+task.feedback.text)<4000
    engine,_=fixture(tmp_path,tokens,[],task=task,config=RunConfig(token_budget=None,
        summary_budget_fraction=None,summary_time_fraction=None))
    shutil.copytree(package/'initial',engine.root,dirs_exist_ok=True)
    engine.tools.runner=Seatbelt(engine.tools.scope,tmp_path/'commands')
    files=[p for p in (package/'reference').rglob('*') if p.is_file()]
    writes=[call('write',{'path':str(p.relative_to(package/'reference')),'content':p.read_text()},str(i)) for i,p in enumerate(files)]
    from goalkeeper.provider import ScriptedProvider
    engine.provider=ScriptedProvider([writes,[call('request_delivery',{'summary':'offline authored reference','check_ids':[],'unresolved':[]},'delivery')]])
    state=await engine.run()
    assert state.phase=='delivered' and state.feedback_delivered and state.goal_version==2
    verdict=await score(engine.store.directory/'delivery/snapshot',package/'grade.py',tmp_path/'grading')
    assert verdict['passed'] and len(verdict['checks'])==36
    assert state.config.token_budget is None and state.config.seconds_budget==1800
    assert engine.store.load().phase=='delivered'
    contract=json.loads((package/'contract.json').read_text())
    assert contract['human_duration_calibrated'] is False
    config=copy.deepcopy(state.config.model_dump())
    assert config['summary_budget_fraction'] is config['summary_time_fraction'] is None
