"""Scoped recommendation skills and an explicitly synthetic, read-only log service."""
import json
import re
from datetime import datetime
from pathlib import Path

ASSETS = Path(__file__).parent / 'demo_assets'


def route(text):
    if not re.search(r'推荐|负反馈|复购|敏感品|敏感商品|bad\s*case', text, re.I):
        return None
    if re.search(r'介绍|翻译|写一段|是什么|设计.*方案', text) and not re.search(r'\bPIN\b|\bSKU\b|skuid|推荐位', text, re.I):
        return None
    if re.search(r'负反馈|不感兴趣|品类不喜欢|图片引起不适|点击.*已经买了', text):
        return 'negative-feedback'
    if re.search(r'敏感品|敏感商品|低质商品', text):
        return 'sensitive-product'
    if re.search(r'已购|买过|刚买|下单|复购|订单', text):
        return 'repurchase'
    if re.search(r'同\s*SPU|同图|重复推荐|重复曝光', text, re.I):
        return 'same-image'
    return None


def catalog():
    return json.loads((ASSETS / 'data/cases.json').read_text())['cases']


def select_cases(query, kind):
    """Honor every supplied selector; a miss must never fall back to another user's case."""
    selectors = {}
    for field, pattern in {
        'pin': r'\bPIN\s*[:：]?\s*([\w-]+)', 'uuid': r'\bUUID\s*[:：]?\s*([\w-]+)',
        'pid': r'推荐位\s*(?:ID)?\s*[:：]?\s*(\d+)',
    }.items():
        if match := re.search(pattern, query, re.I):
            selectors[field] = match[1]
    if date := re.search(r'(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?', query):
        selectors['date'] = f'{date[1]}-{int(date[2]):02}-{int(date[3]):02}'
    ids = set(re.findall(r'(?<![\w])\d{11,15}(?![\w])', query))
    for spu in re.findall(r'SPU\s*[（(:：]?\s*(\d+)', query, re.I):
        ids.discard(spu)
    candidates = [c for c in catalog() if c['skill'] == kind
                  and all(c[k] == value for k, value in selectors.items())]
    if ids:
        # Feedback SKU is an additional behavior selector, not a recommended result SKU.
        feedback = {'10009234000121'} if kind == 'negative-feedback' else set()
        candidates = [c for c in candidates if ids <= set(c['skus']) | feedback]
    elif not any(k in selectors for k in ('pin', 'uuid')):
        return []  # Do not silently answer a generic problem using a seeded user's logs.
    hours = re.search(r'(上午|下午|晚上)?\s*(\d{1,2})(?:点|时|:)(\d{2})?\s*[-—–到至]\s*(\d{1,2})(?:点|时|:)(\d{2})?', query)
    if hours:
        start, end = int(hours[2]), int(hours[4])
        if hours[1] in {'下午', '晚上'}:
            start += 12 if start < 12 else 0
            end += 12 if end < 12 else 0
        records = read_table('requests')
        candidates = [c for c in candidates if any(
            row['request_id'] in c['request_ids'] and start <= datetime.fromisoformat(row['at']).hour < end
            for row in records)]
    return candidates


def read_table(name):
    return [json.loads(line) for line in (ASSETS / 'data' / (name + '.jsonl')).read_text().splitlines() if line]


def instructions(state):
    kind = route(state.task.objective)
    if not kind:
        return ''
    skill = (ASSETS / 'skills' / kind / 'SKILL.md').read_text()
    return '\n已自动加载推荐排查技能：'+kind+'。\n'+skill+'''
本程序已提供query_recommendation_case只读查询工具。先传入用户原问题，自动查请求、漏斗、行为、画像与规则；
traceId等由查询工具关联，不要求用户另找日志。结果标注为本地模拟证据，不能声称连接了生产系统。
查询命中后以证据编号解释归因，区分已确认的链路事实、推测和建议；不要虚构置信度数字。
如果没有匹配记录，说明哪些查询条件未命中，不用另一案例冒充，不执行内网、SSH或真实数据库访问。
回答包含：案例范围、核心结论、关键信号表、至少两条对应证据、排除的其他解释、建议验证方向。
有足够证据时直接通过request_delivery提交最终答案；本地模拟/未发布修复属于limitations，
不属于未完成诊断，不能为此追问生产权限或新增发布任务。无需生成文件，除非用户明确要求。'''


def definitions(state):
    if not state or not route(state.task.objective):
        return []
    return [{'type': 'function', 'name': 'query_recommendation_case',
             'description': '查询预置的本地模拟推荐日志，自动关联请求、过滤漏斗、订单或反馈、画像与规则。'
                            '只读且不连接生产系统。query应保留用户原问题的时间、推荐位、PIN/UUID和SKU。',
             'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                            'required': ['query'], 'additionalProperties': False}}]


def query(engine, text):
    if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
        raise ValueError('请传入有效的案例问题。')
    kind = route(engine.state.task.objective)
    matches = select_cases(text, kind)
    data = {'data_source': 'GoalKeeper 本地模拟推荐服务（演示数据，未连接生产系统）',
            'synthetic': True, 'skill': kind, 'goal_version': engine.state.goal_version,
            'status': 'matched' if len(matches) == 1 else 'ambiguous' if matches else 'not_found', 'cases': []}
    for case in matches:
        result = {'case_id': case['id'], 'date': case['date'], 'pid': case['pid'],
                  'skus': case['skus'], 'spu': case['spu'], 'evidence': {}}
        for name in ('requests', 'funnel', 'behaviors', 'profiles', 'rules'):
            records = [r for r in read_table(name) if r['request_id'] in case['request_ids']]
            result['evidence'][name] = [{k: v for k, v in r.items() if k not in {'pin', 'uuid'}} for r in records]
        data['cases'].append(result)
    if not matches:
        data['note'] = '该查询条件没有匹配的模拟日志。未查询生产数据，也不能据此判断生产链路无请求。'
    data['reference'] = engine.store.archive(data)
    engine.save('recommendation_case_queried', {'skill': kind, 'case_ids': [c['id'] for c in matches],
                                              'reference': data['reference'], 'synthetic': True})
    engine.emit('task_progress', {'title': '已查询本地模拟推荐日志', 'skill': kind,
                                 'case_count': len(matches), 'synthetic': True})
    return data


def queried_current_scope(state):
    names = {i.get('call_id'): i.get('name') for i in state.history if i.get('type') == 'function_call'}
    for item in state.history:
        if item.get('type') != 'function_call_output' or names.get(item.get('call_id')) != 'query_recommendation_case':
            continue
        try:
            value = json.loads(item['output'])
            if value.get('goal_version') == state.goal_version:
                return True
        except (ValueError, KeyError, TypeError):
            pass
    return False


def delivery_guard(state, args):
    if not route(state.task.objective):
        return None
    if not queried_current_scope(state):
        return '请先调用query_recommendation_case查询当前案例，再依据返回的证据提交归因。'
    if not re.search(r'模拟|演示', args.get('answer', '')):
        return '最终答案需要明确标注基于本地模拟日志，不能把演示证据写成真实线上调查。'
    return None
