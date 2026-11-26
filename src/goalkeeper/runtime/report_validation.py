"""Check the real HTML artifact at delivery; do not claim a factual or independent review."""
from bs4 import BeautifulSoup
from .report_flow import source_key


def validate_report(engine, files, unresolved):
    html = [p for p in files if p.lower().endswith('.html')]
    if not html:
        return {'passed': False, 'reason': '本任务要求HTML文件；请生成文件并列入交付，不能只返回报告计划或文字。'}
    report = engine.state.report
    known = {source_key(u) for m in report['materials'].values()
             for u in ([m['url']] if m.get('url') else [s['url'] for s in m.get('sources', [])])}
    details = []
    for name in html:
        path = engine.tools.scope.path(name)
        soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
        text = soup.get_text(' ', strip=True)
        if not soup.html or not soup.body or not soup.title or len(text) < 600:
            return {'passed': False, 'reason': 'HTML结构或报告正文不完整，请补齐实际文件。'}
        ids = {e['id'] for e in soup.select('[id]')}
        broken = [a['href'] for a in soup.select('a[href^="#"]') if a['href'][1:] not in ids]
        if broken:
            return {'passed': False, 'reason': 'HTML目录或引用锚点不存在：' + ', '.join(broken[:4])}
        links = {source_key(a['href']) for a in soup.select('a[href]') if a['href'].startswith(('https://', 'http://'))}
        traced = links & known
        if not traced:
            return {'passed': False, 'reason': '报告缺少可追溯到实际取得资料的来源链接。'}
        missing = [p['name'] for p in report['findings'] if p['name'] not in text]
        if missing and not unresolved:
            return {'passed': False, 'reason': '已记录的比较对象未出现在报告中：' + '、'.join(missing)}
        omitted = [name for name in report.get('required_subjects', []) if name not in text or
                   name not in {p['name'] for p in report['findings']}]
        if omitted and not unresolved:
            return {'passed': False, 'reason': '用户明确要求的比较对象尚未完成：' + '、'.join(omitted)}
        if len(report['findings']) < report['required_products'] and not unresolved and report['mode'] != 'revise':
            return {'passed': False, 'reason': '尚未记录足够代表对象的证据；请补齐事实记录，或明确交付范围缺口。'}
        details.append({'path': name, 'characters': len(text), 'traced_sources': len(traced), 'broken_anchors': broken})
    record = {'id': 'report-check-' + str(len(engine.state.verification)+1), 'argv': ['internal:html_structure_sources'],
              'goal_version': engine.state.goal_version, 'key': engine.key(), 'passed': True,
              'result': {'files': details, 'scope': '文件结构与来源追溯；不代表事实逐句审计或浏览器渲染验收。'}}
    engine.state.verification.append(record)
    engine.save('verification', record)
    return {'passed': True, 'id': record['id']}
