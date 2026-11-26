"""Shared-entry contracts: real local artifacts and scripted models only."""
import json
import time

import pytest

from goalkeeper import cli, evaluation
from goalkeeper.engine import Engine
from goalkeeper.models import MODEL, RunConfig, State, Task
from goalkeeper.paths import PROJECT
from goalkeeper.provider import ScriptedProvider
from goalkeeper.runtime.architecture import verify_single_runtime
from goalkeeper.runtime.factory import create_engine
from goalkeeper.runtime.services import RuntimeServices
from goalkeeper.storage import Store


def test_single_runtime_architecture_and_shared_entry_factories():
    assert verify_single_runtime(PROJECT / 'src/goalkeeper')['passed']
    assert cli.create_engine is create_engine and evaluation.create_engine is create_engine
    import goalkeeper.runtime.session as session
    assert session.create_engine is create_engine
    assert not (PROJECT / 'src/goalkeeper/desktop/runner.py').exists()
    assert not (PROJECT / 'src/goalkeeper/desktop/models.py').exists()


@pytest.mark.parametrize('objective,effort', [('今天北京的天气怎么样？', 'low'),
    ('CSV 和 Excel 有什么区别？', 'medium'), ('修复代码并运行测试', 'xhigh'),
    ('给我做一个国内办公Agent的调研报告，生成HTML文件。', 'medium')])
def test_same_task_has_same_requests_at_every_entry(tmp_path, tokens, monkeypatch, objective, effort):
    monkeypatch.setattr("goalkeeper.engine.current_time", lambda: "当前时间：2026-10-11")
    now = time.time()
    monkeypatch.setattr("goalkeeper.runtime.report_flow.time.time", lambda: now)
    requests = []
    for entry in ('desktop', 'cli', 'evaluation'):
        workspace = tmp_path / entry
        workspace.mkdir()
        task = Task(id='same', objective=objective, constraints=[], write_paths=['./'], delivery_mode='interactive')
        state = State(id='same', task=task, config=RunConfig(), workspace=str(workspace),
                      started_at=time.time(), deadline=time.time()+1800)
        engine = create_engine(state, Store(tmp_path / (entry+'-run')), ScriptedProvider([]), tokens,
                               services=RuntimeServices())
        assert type(engine) is Engine
        request = engine.build_request().model_dump(exclude={'cache_key'})
        requests.append(request)
        assert state.config.effort == effort
        assert state.config.token_budget is None and state.config.summary_time_fraction is None
    assert requests[0] == requests[1] == requests[2]


def test_explicit_configuration_overrides_automatic_depth(tmp_path, tokens):
    root = tmp_path / 'workspace'
    root.mkdir()
    state = State(id='fixed', workspace=str(root),
                  task=Task(id='qa', objective='今天北京天气？', constraints=[], write_paths=['./'], delivery_mode='interactive'),
                  config=RunConfig(effort='xhigh'), started_at=time.time(), deadline=time.time()+1800)
    engine = create_engine(state, Store(tmp_path/'run'), ScriptedProvider([]), tokens)
    assert engine.build_request().config.effort == 'xhigh'
    state.queued_messages.append({'id': 'new', 'text': '比较两座城市'})
    assert not state.config.adaptive_effort


def test_legacy_state_read_is_lossless_and_does_not_rewrite_history(tmp_path):
    raw = {'id': 'legacy', 'workspace': str(tmp_path/'workspace'), 'started_at': time.time(),
           'deadline': time.time()+123, 'phase': 'waiting', 'remaining_seconds': 123,
           'task': {'id': 't', 'objective': '今天的天气？', 'constraints': [], 'write_paths': ['./'],
                    'acceptance': [], 'checks': []},
           'config': {'model': MODEL, 'effort': 'low'}, 'events': [{'sequence': 1, 'kind': 'old'}],
           'sequence': 1, 'history': [{'role': 'user', 'content': '原始输入'}]}
    store = Store(tmp_path/'run')
    original = json.dumps(raw, ensure_ascii=False).encode()
    store.path.write_bytes(original)
    state = store.load()
    assert type(state) is State and type(state.task) is Task and type(state.config) is RunConfig
    assert state.schema_version == 2 and state.task.delivery_mode == 'interactive'
    assert state.remaining_seconds == 123 and state.history == raw['history'] and state.events == raw['events']
    assert store.path.read_bytes() == original


async def test_waiting_run_with_main_reply_keeps_time_and_scope(tmp_path, tokens):
    root = tmp_path/'workspace'
    root.mkdir()
    now = time.time()
    state = State(id='wait', workspace=str(root), started_at=now-1000, deadline=now-800,
        remaining_seconds=123, phase='waiting', question='要哪个月份？',
        task=Task(id='t', objective='解释十月销售变化', constraints=[], write_paths=['./'], delivery_mode='interactive'),
        config=RunConfig())
    state.queued_messages.append({'id': 'answer', 'text': '十月'})
    provider = ScriptedProvider([[{'type': 'function_call', 'call_id': 'd', 'name': 'request_delivery',
        'arguments': json.dumps({'summary': '完成', 'answer': '十月说明', 'files': [], 'check_ids': [], 'unresolved': []})}]])
    store = Store(tmp_path/'run')
    engine = create_engine(state, store, provider, tokens)
    await engine.run()
    assert state.phase == 'delivered' and state.deadline-time.time() > 120
    assert state.delivery['completion']['status'] == 'delivered'
    assert state.delivery['files'] == [] and state.delivery['independent_score'] == 'not_run'


async def test_rejected_worker_cannot_overwrite_active_process_identity(tmp_path, tokens):
    root = tmp_path/'workspace'
    root.mkdir()
    state = State(id='owned', workspace=str(root), started_at=time.time(), deadline=time.time()+1800,
                  task=Task(id='t', objective='执行任务', constraints=[], write_paths=['./'], delivery_mode='interactive'),
                  config=RunConfig())
    store = Store(tmp_path/'run')
    store.save(state, 'prepared')
    identity = store.directory/'desktop-processes.json'
    identity.write_bytes(b'active-worker-owned-record')
    with store.lease():
        engine = create_engine(state, store, ScriptedProvider([]), tokens)
        assert identity.read_bytes() == b'active-worker-owned-record'
        with pytest.raises(RuntimeError, match='run_already_active'):
            await engine.run()
        assert identity.read_bytes() == b'active-worker-owned-record'
        assert not engine.tools.runner.probe.exists()
