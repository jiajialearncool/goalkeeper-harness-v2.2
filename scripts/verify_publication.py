"""Validate the public candidate without accounts or real-model requests."""
from pathlib import Path
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit
import unicodedata

ROOT=Path(__file__).resolve().parents[1]
IGNORED={'.git','.venv','__pycache__','.pytest_cache','node_modules'}
ROOT_IGNORED={'work','runtime','build','dist'}


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def heading_ids(text):
    ids=set()
    duplicates={}
    for heading in re.findall(r'^#{1,6}\s+(.+)$',text,re.M):
        heading=re.sub(r'<[^>]+>','',heading).strip().lower()
        slug=''.join(c for c in heading if c in {'-','_'} or unicodedata.category(c)[0] not in {'P','S','C'})
        slug=slug.replace(' ','-')
        count=duplicates.get(slug,0);duplicates[slug]=count+1
        ids.add(slug+(f'-{count}' if count else ''))
    return ids


def public_files():
    return [p for p in sorted(ROOT.rglob('*')) if p.is_file() and not any(x in IGNORED for x in p.relative_to(ROOT).parts)
            and p.relative_to(ROOT).parts[0] not in ROOT_IGNORED
            and not any(x.endswith('.egg-info') for x in p.relative_to(ROOT).parts)
            and not ('examples' in p.relative_to(ROOT).parts and 'generated' in p.relative_to(ROOT).parts)]


def check():
    failures=[]
    files=public_files()
    manifest=json.loads((ROOT/'docs/evidence/source-manifest.json').read_text())
    exported={str(p.relative_to(ROOT)) for p in files}
    for row in manifest['files']:
        p=ROOT/row['path']
        if not p.is_file() or digest(p)!=row['sha256']:failures.append('source hash mismatch: '+row['path'])
        if row['path'] not in exported:failures.append('source omitted from public inventory: '+row['path'])
    python_sources=list((ROOT/'src').rglob('*.py'))
    if len(python_sources)!=68:failures.append('expected 68 Python source files')
    # This guard reads Python AST, not imported host plugins.
    import sys
    sys.path.insert(0,str(ROOT/'src'))
    from goalkeeper.runtime.architecture import verify_single_runtime
    architecture=verify_single_runtime(ROOT/'src/goalkeeper')
    figures=json.loads((ROOT/'docs/evidence/figures-manifest.json').read_text())
    if digest(ROOT/'docs/figures/gpt_figures_final.mmd')!=figures['original_sha256']:
        failures.append('original figure source changed')
    figure_bodies={}
    for f in figures['figures']:
        p=ROOT/f['path']
        if digest(p)!=f['sha256']:failures.append('figure body changed: '+str(f['number']))
        figure_bodies[p.read_text().rstrip()]=f['number']
    found=[];links=0
    for p in files:
        if p.suffix not in {'.md','.json','.py','.toml','.txt','.mmd','.html','.svg','.cjs'}:continue
        text=p.read_text()
        sensitive_pattern=r'/' + r'Users/|/private/var/' + r'folders/|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}'
        if re.search(sensitive_pattern,text):
            failures.append('private path or credential-like literal: '+str(p.relative_to(ROOT)))
        if p.suffix!='.md':continue
        authored='reports' not in p.relative_to(ROOT).parts
        if authored and re.search(r'不是[^。\n]{0,80}而是',text):failures.append('disallowed prose: '+str(p.relative_to(ROOT)))
        for body in re.findall(r'```mermaid\n(.*?)```',text,re.S):
            n=figure_bodies.get(body.rstrip())
            if n is None:failures.append('unapproved Mermaid body: '+str(p.relative_to(ROOT)))
            else:found.append(n)
        # Ignore code fences when checking prose links.
        prose=re.sub(r'```.*?```','',text,flags=re.S)
        for href in re.findall(r'!?\[[^\]\n]*\]\(([^)\n]+)\)',prose):
            href=href.strip().strip('<>')
            if urlsplit(href).scheme:continue
            bare,_,fragment=href.partition('#')
            target=(p.parent/unquote(bare)).resolve() if bare else p
            if not target.is_relative_to(ROOT) or not target.exists():failures.append('broken link: '+str(p.relative_to(ROOT))+' -> '+href)
            elif target.is_dir():failures.append('directory used as file link: '+href)
            elif fragment:
                fragment=unquote(fragment)
                if target.suffix=='.md' and fragment not in heading_ids(target.read_text()):
                    failures.append('broken heading anchor: '+str(p.relative_to(ROOT))+' -> '+href)
                if target.suffix=='.py' and (not re.fullmatch(r'L\d+',fragment) or not 1<=int(fragment[1:])<=len(target.read_text().splitlines())):
                    failures.append('invalid source line: '+href)
            links+=1
    if sorted(found)!=[1,2,3,4,5]:failures.append('expected all five approved Mermaid blocks exactly once: '+str(found))
    d=json.loads((ROOT/'docs/evidence/reports/historical-16/benchmark-results.json').read_text())
    statistics=[]
    for total in d['totals']:
        runs=[x for x in d['runs'] if x['group']==total['group']]
        for key in ['requests','known_tokens','unknown_usage_requests']:
            if sum(x[key] for x in runs)!=total[key]:failures.append('historical total mismatch: '+total['group']+'/'+key)
        if abs(sum(x['wall_seconds'] for x in runs)-total['wall_seconds'])>1e-5:failures.append('historical time mismatch')
    for key,subset in d['comparisons'].items():
        ids=set(subset['task_ids'])
        for group,total in subset['totals'].items():
            runs=[x for x in d['runs'] if x['group']==group and x['task'] in ids]
            if any(x['unknown_usage_requests'] for x in runs):failures.append('incomplete usage in efficiency subset')
            for field in ['requests','known_tokens']:
                if sum(x[field] for x in runs)!=total[field]:failures.append('subset sum mismatch: '+key+'/'+group+'/'+field)
            if abs(sum(x['wall_seconds'] for x in runs)-total['wall_seconds'])>1e-5:failures.append('subset time mismatch')
        base=subset['totals']['goalkeeper']
        for group,reductions in subset['goalkeeper_reduction_percent'].items():
            for field,value in reductions.items():
                computed=round((1-base[field]/subset['totals'][group][field])*100,1)
                if computed!=value:failures.append('efficiency percentage mismatch')
        statistics.append(key)
    terminal_checked=[]
    for batch in [2,3,4]:
        data=json.loads((ROOT/f'docs/evidence/reports/terminal-bench/batch{batch}-2.2.4-summary.json').read_text())
        report=(ROOT/f'docs/evidence/reports/terminal-bench/batch{batch}-2.2.4-report.md').read_text()
        table=report.split('## 每题')[0]
        groups=data.get('summary',data.get('groups'))
        for name,row in groups.items():
            label={'goalkeeper-2.2.4':'GoalKeeper','package-harness':'Harness','codex-native':'Codex','pi-native':'Pi'}[name]
            line=next(x for x in table.splitlines() if x.startswith('| ') and label in x and '/' in x)
            cells=[x.strip() for x in line.strip('|').split('|')]
            if cells[1]!=f'{row["passed"]}/{row["total"]}':failures.append('Terminal-Bench score mismatch')
            match=re.search(r'(\d+)分([0-9.]+)秒',cells[2])
            seconds=int(match[1])*60+float(match[2])
            actual=row.get('wall_seconds',row.get('seconds'))
            if abs(seconds-actual)>0.06:failures.append('Terminal-Bench duration mismatch')
            tokens=int(re.sub(r'[^0-9]','',cells[3]))
            if tokens!=row.get('tokens',row.get('known_tokens')):failures.append('Terminal-Bench tokens mismatch')
            if int(cells[4])!=row['model_calls']:failures.append('Terminal-Bench calls mismatch')
            if ('≥' in cells[3])==row['usage_complete']:failures.append('Terminal-Bench unknown-usage label mismatch')
        terminal_checked.append(batch)
    summary={'passed':not failures,'failures':failures,'python_source_files':len(python_sources),
             'source_files_with_hash':len(manifest['files']),'architecture':architecture,
             'approved_mermaid_blocks':found,'local_file_links_checked':links,
             'historical_runs_recalculated':len(d['runs']),'efficiency_subsets_recalculated':statistics,
             'terminal_summary_batches_crosschecked':terminal_checked,
             'external_links':'checked separately; not covered by local path check',
             'paid_model_evaluations_run':False}
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return summary


if __name__=='__main__':
    result=check()
    raise SystemExit(0 if result['passed'] else 1)
