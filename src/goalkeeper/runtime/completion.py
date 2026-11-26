"""Shared completion contract. Trusted user scope, evidence gates and immutable legacy reviews.

This is not a semantic judge: unknown gaps remain unmet. Known scope limits are only
non-blocking when the user did not request that capability. File gates cannot be waived
by model prose. No model, login or network is used by this module.
"""
import hashlib
import json
import re

from .files import read_file

ARTIFACTS = {'word': ('.docx',), 'pdf': ('.pdf',), 'html': ('.html', '.htm'),
             'excel': ('.xlsx',), 'slides': ('.pptx',), 'chart': ('.png', '.jpg', '.jpeg', '.svg')}
COMPLETION_VERSION = 4
LIMITS = [
    ('profit', r'净利润|经营费用|工资|房租', r'(计算|分析|测算|核算|统计|给出).{0,16}净利润|净利润.{0,12}(计算|分析|报告|汇报|多少)|完整经营费用'),
    ('cause', r'退款原因|售后根因|活动因果|订单级归因|SKU|批次', r'(确认|查明|证明|追溯|分析).{0,16}(根因|因果|退款原因|批次)|退款根因|订单级归因'),
    ('production', r'生产系统|真实线上|线上影响|完整Trace|有意豁免', r'线上.{0,8}(确认|核验|验证|根因)|确认.{0,8}线上|连接.{0,8}生产|核验.{0,8}生产'),
    ('release', r'未执行.{0,12}(修改|修复|发布|部署)|未部署|未发布|未(?:实施|完成).{0,8}(线上|生产).{0,8}修复',
     r'(部署|发布|上线|修改配置|修复配置)|(?:线上|生产).{0,8}修复|修复.{0,8}(?:线上|生产)'),
    ('approval', r'人工.{0,8}(确认|验收)|业务方.{0,8}(确认|审批)', r'必须.{0,8}人工|取得.{0,8}(确认|审批)|人工验收'),
    ('research', r'未公开.{0,8}价格|未实测|未进行.{0,8}实测|不提供.{0,8}(排名|成功率)', r'实测|成功率|效果排名|精确.{0,8}价格'),
    ('forecast', r'明天.{0,10}(预报|天气)|明日.{0,10}(预报|天气)|实时观测', r'明天|明日|今明|实况|实时.{0,6}(天气|气温)|实时观测'),
    ('extended_diagnosis', r'设计意图|总体影响范围|配置变更|豁免记录|细粒度Trace|跨请求(?:去重|频控)|缓存明细',
     r'设计意图|总体影响范围|配置变更|豁免记录|细粒度Trace|跨请求(?:去重|频控)|缓存明细'),
    ('diagnostic_component', r'具体(?:故障)?(?:组件|环节|节点)|具体是.{0,16}(?:同步|缓存|消费)',
     r'(?:定位|确认|查明|找出|确定|排查).{0,24}具体(?:故障)?(?:组件|环节|节点)|哪个(?:组件|环节|节点)'),
    ('recovery_validation', r'(?:未|无法|不能|不证明|不足以).{0,24}(?:恢复|后续请求|过滤效果|画像更新)|(?:恢复|后续请求|过滤效果|画像更新).{0,16}(?:未|无法|不能)',
     r'(?:验证|确认|核查|核验|检查|复核).{0,24}(?:恢复|后续请求|过滤效果|画像更新)'),
]

GAP = r'缺少|缺失|无法|不能|未|不提供|尚需|需要补充|失败|损坏|空白页|没(?:有)?完成|未做完'
CORE_WORK = (r'用户(?:明确)?(?:指定|要求)(?:的)?(?:输入|文件|资料|数据|产物|分析|比较|诊断|结论)|'
             r'(?:关键|必要|核心)(?:分析|诊断|结论|证据|输入|资料)')
CORE_GAP = (r'(?:' + CORE_WORK + r')[^，。；\n]{0,18}(?:未完成|没完成|未做完|未取得|缺少|缺失|不足|无法(?:读取|取得|提供|完成)|不能(?:读取|完成)|损坏|失败)|'
            r'(?:缺少|缺失|无法取得|未取得)[^，。；\n]{0,12}(?:' + CORE_WORK + r')|'
            r'(?:分析|比较|对比|诊断|答案|结论|任务)(?:工作|内容|结果)?(?:尚)?(?:未完成|没完成|未做完)|'
            r'(?:Word|DOCX|PDF|HTML|图表|文件|源表).{0,16}(?:未生成|缺失|损坏|不能读取|无法读取)|'
            r'测试失败|检查失败|预览失败|空白页')


def user_text(text):
    text = text.split('\n当前工作区文件引用：')[0].split('\n当前图片附件：')[0]
    return re.sub(r'^@[^\n]+\n', '', text).strip()


def derive(text, source='user'):
    """Capture user wording and mechanically checkable obligations, never assistant plans."""
    text = user_text(text)
    digest = hashlib.sha256((source + '\0' + text).encode()).hexdigest()[:12]
    result = [{'id': digest + ':answer', 'kind': 'answer', 'content': text, 'source': source, 'minimum': 1}]
    # Ignore explicit prohibitions and optional follow-up clauses when determining hard scope.
    hard = re.sub(r'(?:不要|不用|无需|不需要|不要求|可以不)[^，。；\n]*', '', text)
    output = bool(re.search(r'生成|制作|输出|交付|导出|保存|写入|增加|新增|追加|做.{0,12}报告', hard))
    if output:
        for kind, pattern in [('word', r'Word|DOCX'), ('pdf', r'PDF'), ('html', r'HTML'), ('excel', r'Excel|XLSX'), ('slides', r'PPTX|PPT|PowerPoint')]:
            # A mentioned source spreadsheet is not a requested output workbook.
            output_type = re.search(r'(生成|输出|交付|导出|制作|保存|写入|一份|转成|转为)[^，。；\n]{0,35}(?:' + pattern + r')|(?:' + pattern + r')[^，。；\n]{0,12}(汇报|报告|输出|成果|文件)', hard, re.I)
            if output_type and not re.search(r'(读取|分析|输入|源)(?:的)?\s*(?:' + pattern + r')[^，。；\n]*$', output_type[0], re.I):
                copies = re.search(r'([0-9一二两三四五六七八九十]+)\s*(?:份|个).{0,12}(?:' + pattern + ')', hard, re.I)
                result.append({'id': digest + ':' + kind, 'kind': kind, 'content': text, 'source': source,
                               'minimum': number(copies[1]) if copies else 1})
        chart = re.search(r'(?:生成|制作|输出|交付|增加|新增).{0,8}([0-9一二两三四五六七八九十]+)\s*张.{0,8}(?:图表|图)', hard)
        if chart:
            result.append({'id': digest + ':chart', 'kind': 'chart', 'content': text, 'source': source,
                           'minimum': number(chart[1])})
    if re.search(r'运行.{0,8}(测试|检查|校验)|通过.{0,8}测试|结构校验|核对计算', hard):
        result.append({'id': digest + ':check', 'kind': 'check', 'content': text, 'source': source, 'minimum': 1})
    for i, match in enumerate(re.finditer(r'(?:生成|创建|写入|修改|修复)\s*[`“"]?([\w./-]+\.(?:py|js|ts|tsx|md|txt|csv|json))', hard)):
        result.append({'id': digest + ':file:' + str(i), 'kind': 'file', 'content': text,
                       'source': source, 'minimum': 1, 'path': match[1]})
    return result


def number(value):
    return int(value) if value.isdigit() else {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
                                             '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}.get(value, 1)


def add_requirements(state, text, source='user'):
    for item in derive(text, source):
        if not any(r['content'] == item['content'] and r['kind'] == item['kind'] for r in state.requirements):
            state.requirements.append(item)


def inherit(state, old, text='', source='user', continuation=False):
    """Resume the original contract; human revisions can explicitly replace output formats/counts."""
    import copy
    state.requirements = copy.deepcopy(ensure_requirements(old))
    if continuation:
        return
    # Scope changes are explicit and auditable; never infer them from a model's plan.
    replacement = re.search(r'改为|改成|替换为|只要|只需', text)
    new = derive(text, source)
    dropped = set()
    for kind, extensions in ARTIFACTS.items():
        patterns = '|'.join(re.escape(x.lstrip('.')) for x in extensions)
        pattern = '图表' if kind == 'chart' else patterns
        if re.search(r'(不要|不用|取消|无需)[^，。；\n]{0,12}(?:' + pattern + r')', text, re.I):
            dropped.add(kind)
    new_artifacts = {r['kind'] for r in new if r['kind'] in ARTIFACTS}
    if replacement and new_artifacts:
        if 'chart' in new_artifacts:
            dropped.add('chart')
        if new_artifacts - {'chart'}:
            dropped.update(ARTIFACTS.keys() - {'chart'})
    if not replacement and re.search(r'增加|新增|追加', text):
        for item in new:
            if item['kind'] in ARTIFACTS:
                prior = [r for r in state.requirements if r['kind'] == item['kind']]
                if prior:
                    item['minimum'] += max(r['minimum'] for r in prior)
                    dropped.add(item['kind'])
    state.requirements = [r for r in state.requirements if r['kind'] not in dropped]
    # Earlier wording stays as provenance, but does not reintroduce retired capabilities.
    for r in state.requirements:
        if r['kind'] == 'answer' and replacement:
            r['superseded_scope'] = text
    for item in new:
        if not any(r['content'] == item['content'] and r['kind'] == item['kind'] for r in state.requirements):
            state.requirements.append(item)


def ensure_requirements(state):
    if not state.requirements:
        # Legacy objectives may contain duplicated human steering. No assistant summary is used.
        for i, part in enumerate(re.split(r'\n用户(?:补充要求|要求)：', state.task.objective)):
            add_requirements(state, part, 'legacy:' + str(i))
    return state.requirements


def hard_scope(requirements):
    scope = '\n'.join(r['content'] for r in requirements)
    scope = re.sub(r'(?:不要|不用|无需|不需要|不要求|可以不)[^，。；\n]*', '', scope)
    # Asking for recommendations does not authorize or require their execution.
    scope = re.sub(r'(?:提出|给出|提供|列出|评估|讨论|说明)[^，。；\n]{0,30}(?:建议|验证方向)', '', scope)
    return re.sub(r'(?:可选[:：]?|后续(?:可以|可|建议)|如果需要)[^，。；\n]*', '', scope)


def is_limit(text, requirements):
    """Split clauses so one valid blocker cannot be hidden inside a known limitation."""
    scope = hard_scope(requirements)
    clauses = [c.strip() for c in re.split(r'[；;。\n]', text) if c.strip()]
    disclosures = [c for c in clauses if re.fullmatch(r'(?:运营|经营|活动)记录(?:仅|只)(?:作|作为)背景', c)]
    # A bounded local sample is a stated scope limit, unless a wider sample was requested.
    if not re.search(r'总体|全量|全部请求|完整样本|至少.{0,8}次请求', scope):
        disclosures += [c for c in clauses if re.fullmatch(r'仅(?:有)?[0-9一二两三四五六七八九十百千]+次(?:范围内)?请求', c)]
    meaningful = [c for c in clauses if c not in disclosures and not re.search(r'^(上述|这些|以上).*(注明|说明|报告)', c)]
    if not meaningful:
        return bool(disclosures)
    for clause in meaningful:
        matches = [(key, requested) for key, match, requested in LIMITS if re.search(match, clause, re.I)]
        if (not re.search(r'缺少|缺失|无法|不能|未|不提供|不构成|不属于|尚需|需要补充', clause)
                or not matches or any(re.search(requested, scope, re.I) for _, requested in matches)):
            return False
        # Never downgrade a file/test failure even if it also contains a known limit.
        if re.search(r'(Word|DOCX|PDF|HTML|图表|文件|源表).{0,16}(未生成|缺失|损坏|不能读取|无法读取)|测试失败|检查失败|预览失败|空白页', clause, re.I):
            return False
    return True


def classify_notice(text, field, requirements, structured):
    """Use captured scope and concrete failures, not a generic negative-word veto.

    Valid structured results can carry new limitation wording without a growing
    allowlist. Explicitly requested capabilities and concrete failures still block;
    ambiguous legacy unresolved prose remains unmet. This is not a semantic grader.
    """
    scope = hard_scope(requirements)
    matches = [(key, requested) for key, pattern, requested in LIMITS if re.search(pattern, text, re.I)]
    required = [key for key, requested in matches if re.search(requested, scope, re.I)]
    if re.search(CORE_GAP, text, re.I):
        return 'unmet', 'required_work_or_concrete_failure'
    if re.search(GAP, text) and required:
        return 'unmet', 'explicit_scope:' + ','.join(required)
    if is_limit(text, requirements):
        return 'limitation', 'outside_requested_scope'
    if field == 'limitations' and (structured or not re.search(GAP, text)):
        return 'limitation', 'structured_scope_disclosure' if structured else 'disclosure'
    return 'unmet', 'unclassified_unresolved_or_legacy_gap'


def validate_artifact(root, name, kind):
    """Validate real bytes, not just a suffix or a model's completion claim."""
    import io
    import zipfile
    try:
        raw = read_file(root, name, 20_000_000)
        if not raw:
            return False
        if kind in {'word', 'excel', 'slides'}:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if len(z.infolist()) > 5000 or sum(i.file_size for i in z.infolist()) > 50_000_000:
                    return False
                if z.testzip() is not None:
                    return False
                target = {'word': 'word/document.xml', 'excel': 'xl/workbook.xml', 'slides': 'ppt/presentation.xml'}[kind]
                import xml.etree.ElementTree as ET
                doc = ET.fromstring(z.read(target))
                if kind == 'word':
                    return any(e.tag.endswith('}t') and e.text for e in doc.iter())
                if kind == 'slides':
                    return any(n.startswith('ppt/slides/slide') and n.endswith('.xml') for n in z.namelist())
                return any(e.tag.endswith('}sheet') for e in doc.iter()) and any(
                    n.startswith('xl/worksheets/') and n.endswith('.xml') for n in z.namelist())
        if kind == 'pdf':
            from pypdf import PdfReader
            return len(PdfReader(io.BytesIO(raw)).pages) > 0
        if kind == 'html':
            from bs4 import BeautifulSoup
            doc = BeautifulSoup(raw.decode('utf-8'), 'html.parser')
            return bool(doc.html and doc.body and doc.get_text(strip=True))
        if kind == 'chart':
            if name.lower().endswith('.svg'):
                import xml.etree.ElementTree as ET
                return ET.fromstring(raw).tag.endswith('svg')
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
            return True
    except Exception:
        # Third-party parsers have distinct exceptions. Treat every parse failure as invalid.
        return False
    return True


def review(state, args, root, files, valid_checks):
    requirements = ensure_requirements(state)
    known = {r['id']: r for r in requirements}
    limits = []
    suggestions = list(args.get('suggestions', []))
    unmet = []
    # Structured result entries may only target captured user requirements.
    results = args.get('requirement_results')
    if results is not None:
        if not isinstance(results, list):
            raise ValueError('requirement_results 必须为逐项结果列表。')
        seen = set()
        for item in results:
            if (not isinstance(item, dict) or set(item) != {'id', 'status', 'evidence', 'reason'}
                    or item['id'] not in known or item['id'] in seen
                    or item['status'] not in {'completed', 'pending', 'failed'}
                    or not isinstance(item['reason'], str) or not isinstance(item['evidence'], list)
                    or any(not isinstance(ref, str) for ref in item['evidence'])):
                raise ValueError('逐项结果必须对应本次用户要求，状态和证据字段无效。')
            seen.add(item['id'])
            if item['status'] != 'completed':
                unmet.append(item['reason'] or '尚未完成：' + known[item['id']]['content'])
            elif not item['evidence'] or any(ref not in {'answer', *files, *valid_checks} for ref in item['evidence']):
                raise ValueError('完成证据必须来自本次答案、真实交付文件或有效检查记录。')
        if seen != set(known):
            raise ValueError('逐项结果未覆盖全部用户要求，不能按遗漏的清单交付。')
    notices = []
    for field in ('unresolved', 'limitations'):
        for text in args.get(field, []):
            for clause in [c.strip() for c in re.split(r'[；;。\n]', text) if c.strip()]:
                if re.search(r'^(上述|这些|以上).*(注明|说明|报告)', clause):
                    continue
                category, reason = classify_notice(clause, field, requirements, results is not None)
                (limits if category == 'limitation' else unmet).append(clause)
                notices.append({'text': clause, 'submitted_field': field, 'category': category, 'reason': reason})
    failures = []
    outcomes = []
    for req in requirements:
        kind = req['kind']
        evidence = []
        reason = ''
        if kind in ARTIFACTS:
            evidence = [f for f in files if f.lower().endswith(ARTIFACTS[kind]) and validate_artifact(root, f, kind)]
            if len(evidence) < req['minimum']:
                reason = f"用户要求的 {kind} 产物尚未完成或无效：需要 {req['minimum']}，实际有效 {len(evidence)}。"
        elif kind == 'file':
            evidence = [f for f in files if f == req['path']]
            if not evidence or not read_file(root, req['path']):
                reason = '用户要求的文件尚未交付：' + req['path']
        elif kind == 'check':
            evidence = list(valid_checks)
            if not evidence:
                reason = '用户要求的实际检查尚未通过或检查已失效。'
        else:
            evidence = ['answer']
        if reason:
            failures.append(reason)
            unmet.append(reason)
        outcomes.append({**req, 'status': 'pending' if reason else 'completed', 'evidence': evidence, 'reason': reason})
    if results is not None:
        for item in outcomes:
            declared = next(x for x in results if x['id'] == item['id'])
            if declared['status'] != 'completed':
                item.update(status=declared['status'], reason=declared['reason'])
    if unmet:
        # Unknown legacy blockers cannot be silently declared complete.
        for item in outcomes:
            if item['kind'] == 'answer':
                item.update(status='pending', reason='存在尚未解决或尚未分类的事项。')
    return {'version': COMPLETION_VERSION, 'status': 'incomplete' if unmet else 'delivered',
            'requirements': outcomes, 'unmet': list(dict.fromkeys(unmet)),
            'limitations': list(dict.fromkeys(limits)), 'suggestions': suggestions,
            'failures': failures, 'legacy': results is None, 'notice_classifications': notices}


def submitted_notices(state):
    """Recover original fields only from a matching, accepted archived delivery call.

    Old reviews moved limitation prose into unresolved. Reusing those derived fields
    would perpetuate their mistake. Match the answer/files/requirements and successful
    tool result; ambiguous records retain the saved classification conservatively.
    """
    delivery = state.delivery
    for call in reversed(state.history):
        if call.get('type') != 'function_call' or call.get('name') != 'request_delivery':
            continue
        try:
            args = json.loads(call['arguments'])
            outcome = next(item for item in state.history if item.get('type') == 'function_call_output'
                           and item.get('call_id') == call['call_id'])
            result = json.loads(outcome['output'])
            if (result.get('passed') is True and args.get('answer', args.get('summary')) == delivery['answer']
                    and args.get('summary') == delivery['summary']
                    and args.get('files', delivery.get('files', [])) == delivery.get('files', [])
                    and args.get('requirement_results') == delivery.get('requirement_results')
                    and all(isinstance(args.get(field, []), list) and all(isinstance(x, str) for x in args.get(field, []))
                            for field in ('unresolved', 'limitations'))):
                return {field: args.get(field, []) for field in ('unresolved', 'limitations')}
        except (ValueError, TypeError, KeyError, StopIteration, AttributeError):
            continue
    return None


def review_saved(state, store):
    """Review immutable delivery bytes. A missing/tampered snapshot is never proof of completion."""
    from .files import workspace_snapshot
    delivery = state.delivery
    root = store.directory / 'delivery/snapshot'
    if not delivery or not isinstance(delivery.get('snapshot'), dict) or workspace_snapshot(root) != delivery['snapshot']:
        raise ValueError('原交付快照缺失或已变化，不能自动确认完成。')
    records = {r['id']: r for r in state.verification if r.get('passed')
               and r.get('goal_version', state.goal_version) == delivery.get('goal_version', state.goal_version)}
    checks = [cid for cid in delivery.get('checks', []) if cid in records]
    args = dict(delivery)
    if original := submitted_notices(state):
        args.update(original)
    if len(checks) != len(delivery.get('checks', [])):
        args['unresolved'] = [*args.get('unresolved', []), '原交付引用的检查记录不存在或未通过。']
    files = delivery.get('files', delivery.get('changed_files', []))
    if not isinstance(files, list) or any(f not in delivery['snapshot'] for f in files):
        raise ValueError('无法确定原交付文件清单，保留原状态。')
    result = review(state, args, root, files, checks)
    result['snapshot_identity'] = hashlib.sha256(str(sorted(delivery['snapshot'].items())).encode()).hexdigest()
    return result


def validate_wait(state, args):
    requirements = ensure_requirements(state)
    question = args['question']
    if not isinstance(question, str) or not question.strip():
        raise ValueError('问题不能为空。')
    if is_limit(question, requirements):
        return {'waiting_for_user': False, 'reason': '这属于原要求范围之外的数据限制；请交付可完成的内容，并在答复说明限制。'}
    rid = args.get('requirement_id')
    if rid is not None:
        if rid not in {r['id'] for r in requirements} or not all(
                isinstance(args.get(k), str) and args[k].strip() for k in ('missing_input', 'reason')):
            raise ValueError('补充资料必须关联用户要求，并说明缺失输入和阻塞原因。')
        if is_limit('缺少' + args['missing_input'], requirements):
            return {'waiting_for_user': False, 'reason': '补充输入与用户必需要求不符，不暂停任务。'}
    state.waiting_requirement = {'requirement_id': rid, 'missing_input': args.get('missing_input'),
                                 'reason': args.get('reason'), 'legacy': rid is None}
    state.question = question
    return {'waiting_for_user': True, 'question': question}
