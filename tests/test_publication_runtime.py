"""Release-preparation checks added now, not historical benchmark outcomes."""
import json
import sys
import time

import pytest

from goalkeeper.context import split_for_compaction, units
from goalkeeper.models import Check, RunConfig, State, Task
from goalkeeper.provider import ScriptedProvider
from goalkeeper.runtime.completion import add_requirements, review
from goalkeeper.runtime.factory import create_engine
from goalkeeper.sandbox import PermissionDenied
from goalkeeper.storage import Store


def engine(tmp_path, tokens, task=None):
    workspace=tmp_path/'workspace'
    workspace.mkdir()
    now=time.time()
    task=task or Task(id='interactive',objective='生成 report.csv，并说明结果。',constraints=[],
                     write_paths=['./'],delivery_mode='interactive')
    state=State(id='public-test',task=task,config=RunConfig(),workspace=str(workspace),
                started_at=now,deadline=now+1800)
    return create_engine(state,Store(tmp_path/'run'),ScriptedProvider([]),tokens)


async def test_interactive_rejects_missing_file_then_saves_read_only_snapshot(tmp_path,tokens):
    e=engine(tmp_path,tokens)
    args={'summary':'完成','answer':'已整理数据。','files':[], 'check_ids':[], 'unresolved':[]}
    first=await e.delivery(args)
    assert not first['passed'] and not e.state.delivery
    await e.tools.execute('write',{'path':'report.csv','content':'category,total\nA,10\n'},30)
    result=await e.delivery({**args,'files':['report.csv']})
    frozen=e.store.directory/'delivery/snapshot/report.csv'
    assert result['passed'] and e.state.delivery['completion']['status']=='delivered'
    assert e.state.delivery['verification']=='unverified'
    assert frozen.read_bytes()==(e.root/'report.csv').read_bytes()
    assert frozen.stat().st_mode & 0o222 == 0


async def test_checked_delivery_runs_real_check_and_cannot_modify_check_file(tmp_path,tokens):
    task=Task(id='checked',objective='写入结果',constraints=[],acceptance=['result.txt内容为READY'],
              write_paths=['./'],protected_paths=['check.py'],
              checks=[Check(id='ready',argv=[sys.executable,'check.py'],timeout=10)])
    e=engine(tmp_path,tokens,task)
    (e.root/'check.py').write_text("from pathlib import Path\nassert Path('result.txt').read_text() == 'READY'\n")
    (e.root/'result.txt').write_text('WRONG')
    args={'summary':'完成','answer':'结果已交付','check_ids':[],'unresolved':[]}
    assert not (await e.delivery(args))['passed']
    (e.root/'result.txt').write_text('READY')
    assert (await e.delivery(args))['passed']
    assert e.state.delivery['checks']==['ready']
    assert [r['passed'] for r in e.state.evidence]==[False,True]
    with pytest.raises(PermissionDenied):
        e.tools.scope.path('check.py',write=True)


async def test_compressed_history_is_not_automatically_readable_by_archive_tool(tmp_path,tokens):
    e=engine(tmp_path,tokens)
    reference=e.store.archive([{'role':'user','content':'older conversation'}])
    with pytest.raises(ValueError):
        await e.tools.execute('read_archive',{'reference':reference},30)
    assert e.store.read(reference)


async def test_registered_document_can_be_read_back(tmp_path,tokens):
    e=engine(tmp_path,tokens)
    reference=e.store.archive({'text':'A registered public document.'})
    e.state.material_refs.append(reference)
    result=await e.tools.execute('read_archive',{'reference':reference,'start':1,'lines':10},30)
    assert 'registered public document' in json.dumps(result)


def test_added_requirement_is_kept_outside_compressed_history(tmp_path,tokens):
    e=engine(tmp_path,tokens)
    add_requirements(e.state,e.state.task.objective)
    add_requirements(e.state,'再生成 notes.txt。','new-user-message')
    e.state.history=[]
    assessment=review(e.state,{'answer':'说明'},e.root,[],[])
    assert any('notes.txt' in text for text in assessment['failures'])
    assert any(r.get('path')=='report.csv' for r in e.state.requirements)


async def test_outside_path_is_rejected(tmp_path,tokens):
    e=engine(tmp_path,tokens)
    with pytest.raises(PermissionDenied):
        await e.tools.execute('write',{'path':'../outside.txt','content':'x'},30)
    assert not (tmp_path/'outside.txt').exists()


def test_compaction_never_splits_an_open_tool_call(tokens):
    with pytest.raises(ValueError):
        split_for_compaction([{'type':'function_call','name':'read','call_id':'open','arguments':'{}'}],tokens,1,1000)


def test_checked_task_requires_predefined_acceptance_and_checks():
    with pytest.raises(ValueError):
        Task(id='invalid',objective='任务',constraints=[],write_paths=['./'],delivery_mode='checked')
    task=Task(id='interactive',objective='自然语言任务',constraints=[],write_paths=['./'],delivery_mode='interactive')
    assert task.checks==[] and task.acceptance==[]
