"""Common report context and explicit evidence-to-delivery transitions."""
import copy
import re
import time
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ..context import facts, units
from ..models import ModelRequest
from ..storage import canonical
from .performance import task_profile

NETWORK = {'web_search', 'web_search_many', 'web_read'}


def source_key(url):
    p = urlsplit(url)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not k.startswith('utm_')))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', query, ''))


def day(at=None):
    return datetime.fromtimestamp(at or time.time(), ZoneInfo('Asia/Shanghai')).date().isoformat()


def requested_subjects(objective):
    """Extract named objects only; counts and comparison dimensions are not names."""
    match = re.search(r'(?:比较|对比|调研)\s*([^，。；\n]+)', objective)
    if not match:
        return []
    clause = match[1].strip()
    # The possessive suffix describes what to compare, never another object.
    clause = re.split(r'的(?:定位|核心|能力|功能|价格|成本|适用|优劣|差异|场景)', clause)[0]
    if re.search(r'^[\d一二两三四五六七八九十]+\s*(?:款|个|种)', clause):
        return []
    names = [n.strip(' ：:') for n in re.split(r'和|与|、|及', clause)]
    dimensions = {'定位', '核心能力', '能力', '功能', '适用场景', '场景', '价格', '成本'}
    if len(names) < 2 or any(not n or len(n) > 40 or n in dimensions for n in names):
        return []
    return list(dict.fromkeys(names))


def intent(text, old=None, messages=()):
    messages = [m for m in messages if not m.get('derived')]
    original = next((m['text'] for m in messages if task_profile(m.get('text', '')) == 'research_report'), None)
    if not original and old:
        first = old.task.objective.split('\n用户补充要求：')[0].split('\n用户要求：')[0]
        original = first if task_profile(first) == 'research_report' else None
    editing_report = bool(old and old.delivery and old.delivery.get('files') and
                          re.search(r'压缩|精简|缩短|篇幅|排版|标题|章节|修改|调整|保留', text))
    if task_profile(text) == 'research_report' and not editing_report:
        # Repeating the request is a fresh attempt at the user's scope, not the old model's plan.
        return {'objective': text, 'mode': 'retry' if old else 'new'}
    if not original or re.search(r'代码|部署|深入|全面|医疗|投资|合同', text):
        return None
    if re.search(r'继续|重试|重新|压缩|精简|缩短|篇幅|报告|排版|标题|章节|补充|修改|调整|保留|增加', text):
        additions = list(dict.fromkeys(m['text'] for m in messages if m.get('text') and m['text'] != original))
        additions = [x for x in additions if not re.fullmatch(r'继续(?:任务|此前任务)?[。！!]*', x)]
        if text != original and text not in additions:
            additions.append(text)
        return {'objective': original + ''.join('\n用户要求：' + x for x in additions),
                'mode': 'revise' if old and old.delivery and old.delivery.get('files') else 'continue'}
    return None


def materials_from(old):
    result = copy.deepcopy((old.report or {}).get('materials', {}))
    # Legacy public tool results are evidence; summary decisions and internal request archives are not.
    for event in old.events:
        if event['kind'] == 'operation_result_saved':
            value = event.get('data', {}).get('result')
            if isinstance(value, dict):
                remember(result, value, old.started_at)
    return result


def remember(materials, value, at=None):
    ref = value.get('reference')
    if not isinstance(ref, str) or len(ref) != 64 or not all(x in '0123456789abcdef' for x in ref):
        return
    if not value.get('url') and not value.get('sources'):
        return
    stamp = value.get('fetched_at')
    try:
        timestamp = datetime.fromisoformat(stamp).timestamp() if stamp else at or time.time()
    except (ValueError, TypeError):
        timestamp = at or time.time()
    materials[ref] = {'reference': ref, 'full_reference': value.get('full_reference', ref),
                     'url': value.get('url'), 'title': value.get('title', ''),
                     'sources': value.get('sources', []), 'text': value.get('text', '')[:6500],
                     'at': timestamp, 'day': day(timestamp)}


def prepare(state, store, mode, old=None):
    materials = materials_from(old) if old else {}
    history_ref = store.archive(state.history) if state.history else None
    prior = (old.report or {}) if old else {}
    state.report = {'version': 1, 'mode': mode, 'stage': 'write' if mode == 'revise' else 'collect',
                    'started_at': time.time(), 'collect_until': time.time() + min(240, max(20, (state.deadline-time.time())*.3)),
                    'materials': materials, 'findings': copy.deepcopy(prior.get('findings', [])) if mode == 'revise' else [],
                    'history_reference': history_ref, 'gaps': [], 'required_products': 1,
                    'names_checked': mode == 'revise',
                    'refresh': bool(re.search(r'重新检索|更新资料|最新数据|重新搜索', state.task.objective))}
    explicit = re.search(r'([\d一二两三四五六七八九十]+)\s*(?:款|个|种).{0,12}(?:产品|Agent|智能体)', state.task.objective, re.I)
    if explicit:
        from .completion import number
        state.report['required_products'] = max(1, min(10, number(explicit[1])))
    state.report['required_subjects'] = requested_subjects(state.task.objective)
    state.report['required_products'] = max(state.report['required_products'], len(state.report['required_subjects']))
    state.summary = None
    state.compaction = None
    state.compaction_disabled = None
    state.history = []
    state.pending_tools = []
    state.context_base = facts(state)
    # Previously executing operations remain journaled, but are never replayed.
    for operation in state.operations:
        if operation['status'] == 'executing':
            operation['status'] = 'uncertain'
    for material in materials.values():
        for ref in (material['reference'], material['full_reference']):
            if ref not in state.material_refs:
                state.material_refs.append(ref)


def set_stage(engine, stage, reason):
    if engine.state.report['stage'] == stage:
        return
    engine.state.report.update(stage=stage, stage_reason=reason)
    engine.save('research_stage', {'stage': stage, 'reason': reason,
                                  'products': len(engine.state.report['findings'])})


def update_findings(engine, products, gaps):
    report = engine.state.report
    if not report or not isinstance(products, list) or not 1 <= len(products) <= 10:
        raise ValueError('请记录实际取得的产品事实。')
    available = {source_key(u) for m in report['materials'].values()
                 for u in ([m['url']] if m.get('url') else [s['url'] for s in m.get('sources', [])])}
    checked = []
    for item in products:
        if not isinstance(item, dict) or set(item) != {'name', 'position', 'capabilities', 'scenarios', 'sources', 'limitations'}:
            raise ValueError('产品事实字段不完整。')
        if any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 900
               for k in ('name', 'position', 'capabilities', 'scenarios')):
            raise ValueError('定位、核心能力和场景必须有具体说明。')
        if not isinstance(item['limitations'], str) or len(item['limitations']) > 900:
            raise ValueError('信息缺口字段无效。')
        if not isinstance(item['sources'], list) or not 1 <= len(item['sources']) <= 5 or any(
                not isinstance(u, str) for u in item['sources']):
            raise ValueError('来源必须是本次取得或同日继承的真实资料链接。')
        missing = [u for u in item['sources'] if source_key(u) not in available]
        if missing:
            raise ValueError('来源必须是本次取得或同日继承的真实资料链接；尚未取得：' + '、'.join(missing[:3]))
        checked.append(item)
    if len({p['name'] for p in checked}) != len(checked):
        raise ValueError('产品名称重复。')
    if not isinstance(gaps, list) or any(not isinstance(x, str) or len(x) > 500 for x in gaps):
        raise ValueError('信息缺口必须是短文本列表。')
    report.update(findings=checked, gaps=gaps)
    engine.save('research_findings_saved', {'products': len(checked), 'gaps': gaps})
    if len(checked) >= report['required_products'] and report.get('names_checked'):
        set_stage(engine, 'write', '定位、核心能力及场景已形成可追溯记录，开始写报告')
    return {'recorded': len(checked), 'stage': report['stage'], 'gaps': gaps}


def request_gap(engine, subject, field, reason):
    report = engine.state.report
    if subject not in {p['name'] for p in report['findings']} or field not in {'name', 'position', 'capabilities', 'scenarios'}:
        raise ValueError('默认报告只补查影响定位、核心能力或场景的关键缺口；价格等未知信息写入报告。')
    if not isinstance(reason, str) or len(reason.strip()) < 12:
        raise ValueError('请说明已取得资料中的具体缺失或冲突。')
    if time.time() >= report['collect_until']:
        return {'reopened': False, 'reason': '已为生成与检查文件留出时间；在报告中标注该缺口。'}
    set_stage(engine, 'collect', reason)
    return {'reopened': True}


def definitions():
    product = {'type': 'object', 'properties': {k: {'type': 'string'} for k in
                ('name', 'position', 'capabilities', 'scenarios', 'limitations')},
               'required': ['name', 'position', 'capabilities', 'scenarios', 'sources', 'limitations'],
               'additionalProperties': False}
    product['properties']['sources'] = {'type': 'array', 'items': {'type': 'string'}}
    return [
        {'type': 'function', 'name': 'record_research_findings',
         'description': '收集阶段必须记录有来源的代表产品定位、核心能力和场景；覆盖用户范围后程序转入写作。来源必须来自已取得资料，未知信息明确标注。',
         'parameters': {'type': 'object', 'properties': {'products': {'type': 'array', 'items': product},
                        'gaps': {'type': 'array', 'items': {'type': 'string'}}},
                        'required': ['products', 'gaps'], 'additionalProperties': False}},
        {'type': 'function', 'name': 'request_research_gap',
         'description': '写作阶段仅当已有产品的名称、定位、核心能力或场景存在具体证据冲突时请求补查；不是补齐所有价格或安全字段。',
         'parameters': {'type': 'object', 'properties': {'subject': {'type': 'string'}, 'field': {'type': 'string'},
                        'reason': {'type': 'string'}}, 'required': ['subject', 'field', 'reason'], 'additionalProperties': False}}]


def request(engine, instructions, tools):
    state, report = engine.state, engine.state.report
    visible = []
    size = 0
    for unit in reversed(units(state.history)):
        measured = engine.tokens.count(unit)
        if measured + size <= 6500:
            visible.insert(0, unit)
            size += measured
        # Do not cross omitted transactions to bring stale actions back into focus.
        else:
            break
    materials = sorted(report['materials'].values(), key=lambda m: m['at'], reverse=True)
    cards = [{k: m[k] for k in ('reference', 'url', 'title', 'sources', 'day')} | {'excerpt': m['text'][:1100]}
             for m in materials[:12]]
    from .files import workspace_snapshot
    paths = workspace_snapshot(engine.root)
    files = [{'path': p, 'changed': state.baseline_files is not None and state.baseline_files.get(p) != digest}
             for p, digest in paths.items() if p.lower().endswith('.html')]
    view = {'用户要求': state.task.objective, '约束': state.task.constraints, '当前步骤': report['stage'],
            '代表对象数量': report['required_products'], '已记录事实': report['findings'], '信息缺口': report['gaps'],
            '原始资料': cards, 'HTML文件': files, '当前有效检查': [r['id'] for r in state.verification if r['passed'] and r['key'] == engine.key()]}
    instructions += ('\n先用record_research_findings保存产品事实及实际来源；程序会实际核查当前名称和品牌调整。'
                     '核查返回后修正旧名称、旧定位和相冲突的事实，再次记录依据，达到范围后进入写作。'
                     '当前步骤为write时不要继续收集非关键信息，直接生成或修改HTML并交付。'
                     '事实记录只是证据索引，不代表事实自动验证通过；发现来源矛盾应标注或请求关键补查。'
                     '原始资料与用户要求分开；不把旧模型的摘要、研究范围或待查事项当作用户要求。'
                     '文件交付必须真实生成HTML，价格与未实测效果的缺口在报告中说明。')
    if report['mode'] == 'revise':
        instructions += '\n用户要求修改已有报告：先读取当前HTML，保留真实来源和关键结论，仅按用户要求修改，不重新开展整套调研。'
    return ModelRequest(instructions=instructions, config=state.config, tools=tools,
                        input=[{'role': 'user', 'content': canonical(view)}, *[x for u in visible for x in u]],
                        cache_key=state.id + ':report')
