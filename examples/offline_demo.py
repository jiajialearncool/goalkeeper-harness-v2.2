"""Exercise the original Engine using fixed model responses and real file tools.

This is a publication demo added after 2.2.4. It is not a benchmark or a model test.
"""
from pathlib import Path
import asyncio
import hashlib
import json
import os
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(ROOT / 'work/tokenizer-cache'))

from goalkeeper.cost import Tokens
from goalkeeper.models import RunConfig, State, Task
from goalkeeper.provider import ScriptedProvider
from goalkeeper.runtime.factory import create_engine
from goalkeeper.storage import Store, snapshot
import tiktoken


def call(name, arguments, cid):
    return {'type': 'function_call', 'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False), 'call_id': cid}


async def demo(output=None):
    output = Path(output) if output else ROOT / 'work/offline-demo' / uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / 'workspace'
    workspace.mkdir()
    (workspace / 'sales.csv').write_text('category,amount\n图书,120\n文具,30\n图书,80\n文具,20\n')
    task = Task(id='offline-sales-demo', objective='读取 sales.csv，生成 totals.csv，按品类汇总销售金额，并说明结果。',
                constraints=['仅使用给定模拟数据。'], write_paths=['./'], delivery_mode='interactive')
    now = time.time()
    state = State(id='offline-sales-demo', task=task, config=RunConfig(effort='medium'),
                  workspace=str(workspace), started_at=now, deadline=now+1800,
                  baseline_files=snapshot(workspace))
    missing = {'summary':'已经汇总销售数据', 'answer':'汇总已完成。', 'files':[], 'check_ids':[], 'unresolved':[]}
    complete = {'summary':'已生成按品类汇总文件', 'answer':'图书合计200，文具合计50。totals.csv已交付。',
                'files':['totals.csv'], 'check_ids':[], 'unresolved':[]}
    provider = ScriptedProvider([
        [call('read', {'path':'sales.csv'}, 'read-sales')],
        [call('request_delivery', missing, 'missing-file')],
        [call('write', {'path':'totals.csv','content':'category,total\n图书,200\n文具,50\n'}, 'write-totals')],
        [call('request_delivery', complete, 'complete-file')],
    ])
    store = Store(output / 'run')
    store.save(state, 'demo_prepared', {'model_mode':'scripted', 'not_a_benchmark':True})
    engine = create_engine(state, store, provider, Tokens(tiktoken.get_encoding('o200k_base')))
    result = await engine.run()
    timeline = []
    pending = {}
    for item in state.history:
        if item.get('type') == 'function_call':
            pending[item['call_id']] = item
        elif item.get('type') == 'function_call_output':
            original = pending[item['call_id']]
            value = item['output']
            if isinstance(value,str):
                try: value=json.loads(value)
                except ValueError: pass
            timeline.append({'tool':original['name'],'arguments':json.loads(original['arguments']), 'result':value})
    raw = (workspace / 'totals.csv').read_bytes()
    frozen = store.directory/'delivery/snapshot/totals.csv'
    record = {'demo_type':'original-engine-with-scripted-model', 'benchmark_result':False,
              'source_version':'2.2.4','natural_language_request':task.objective,
              'delivery_mode':task.delivery_mode,'predefined_checks':0,
              'phase':result.phase,'completion_status':state.delivery['completion']['status'],
              'verification':state.delivery['verification'], 'independent_score':state.delivery['independent_score'],
              'timeline':timeline,'requirements':state.delivery['completion']['requirements'],
              'artifact_text':raw.decode(),'artifact_sha256':hashlib.sha256(raw).hexdigest(),
              'snapshot_matches':frozen.read_bytes()==raw, 'snapshot_read_only':frozen.stat().st_mode & 0o222 == 0}
    assert result.phase == 'delivered'
    attempts=[x for x in timeline if x['tool']=='request_delivery']
    assert attempts[0]['result']['passed'] is False and attempts[-1]['result']['passed'] is True
    assert record['snapshot_matches'] and record['snapshot_read_only']
    (output/'demo-result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    print('原版 2.2.4 执行引擎；模型返回为预设脚本；没有真实模型请求。')
    print('第一次交付：缺少 totals.csv，框架拒绝。')
    print('真实文件工具写入 totals.csv 后，第二次交付通过。')
    print('产物：图书=200，文具=50；只读快照与工作文件一致。')
    print('状态：'+record['completion_status']+'；verification='+record['verification']+'；独立评分未运行。')
    print('运行目录：'+str(output.relative_to(ROOT) if output.is_relative_to(ROOT) else output))
    return record


if __name__ == '__main__':
    asyncio.run(demo())
