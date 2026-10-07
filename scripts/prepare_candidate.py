"""Offline task preparation. Never imports or invokes a model provider."""
from pathlib import Path
import json
import textwrap
from goalkeeper.paths import inside_project
from goalkeeper.models import Task

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "evaluation/private/C1-v2"

MODULES = {
    "normalize.py": '''def normalize(row):
    """Return id/title/status/priority; reject malformed rows without mutating input."""
    raise NotImplementedError
''',
    "service.py": '''def apply_batch(state, rows, batch_id):
    """Atomic import; idempotent batch; blank update fields preserve existing values."""
    raise NotImplementedError

def transition_batch(state, changes, batch_id):
    """Additional requirement: atomic optimistic status changes."""
    raise NotImplementedError
''',
    "persistence.py": '''def save(state, path):
    """UTF-8 JSON, atomic replacement; original file survives failure."""
    raise NotImplementedError

def load(path):
    """Load and validate state."""
    raise NotImplementedError
''',
    "report.py": '''def summarize(state):
    """Stable status/priority counts without modifying input."""
    raise NotImplementedError
''',
    "cli.py": '''def main(argv=None):
    """python -m tickets.cli DATABASE import ROWS_JSON_PATH BATCH_ID; report; transition CHANGES_JSON_PATH BATCH_ID. Both payload arguments are UTF-8 JSON file paths, never inline JSON."""
    raise NotImplementedError

if __name__ == "__main__":
    raise SystemExit(main())
'''
}

REFERENCE = {
"normalize.py": '''def normalize(row):
    if not isinstance(row,dict) or set(row)-{'id','title','status','priority'}:
        raise ValueError('row')
    if any(not isinstance(row.get(k,''),str) for k in ('id','title','status','priority')):
        raise ValueError('type')
    out={k:row.get(k,'').strip() for k in ('id','title','status','priority')}
    if not out['id'] or out['status'] not in ('','open','done') or out['priority'] not in ('','low','high'):
        raise ValueError('value')
    return out
''',
"service.py": '''from copy import deepcopy
from .normalize import normalize

def apply_batch(state, rows, batch_id):
    if not isinstance(batch_id,str) or not batch_id.strip() :
        raise ValueError('batch')
    out=deepcopy(state)
    if batch_id in out['batches']:
        return out
    if not isinstance(rows,list): raise ValueError('rows must be array')
    parsed=[normalize(r) for r in rows]
    if len({r['id'] for r in parsed})!=len(parsed):
        raise ValueError('duplicate')
    for r in parsed:
        old=out['tickets'].get(r['id'])
        if old:
            updated={k:r[k] or old[k] for k in ('id','title','status','priority')}
            updated['revision']=old['revision']+1
        else:
            if not all(r.values()):
                raise ValueError('new row incomplete')
            updated={**r,'revision':1}
        out['tickets'][r['id']]=updated
    out['batches'].append(batch_id)
    out['audit'].append({'batch_id':batch_id,'kind':'import','ids':sorted(r['id'] for r in parsed)})
    return out

def transition_batch(state, changes, batch_id):
    if not isinstance(batch_id,str) or not batch_id.strip() :
        raise ValueError('batch')
    out=deepcopy(state)
    if batch_id in out['batches']:
        return out
    if not isinstance(changes,list): raise ValueError('changes must be array')
    seen=set()
    for change in changes:
        if not isinstance(change,dict) or set(change)!={'id','expected_revision','status'}:
            raise ValueError('change')
        ident=change['id']; revision=change['expected_revision']; status=change['status']
        if not isinstance(ident,str) or ident in seen or ident not in out['tickets']:
            raise ValueError('id')
        seen.add(ident)
        if type(revision) is not int or revision!=out['tickets'][ident]['revision'] or status not in ('open','done'):
            raise ValueError('conflict')
    for change in changes:
        item=out['tickets'][change['id']]
        item['status']=change['status']; item['revision']+=1
    out['batches'].append(batch_id)
    out['audit'].append({'batch_id':batch_id,'kind':'transition','ids':sorted(seen)})
    return out
''',
"persistence.py": '''import json
import os
import tempfile
from pathlib import Path

def load(path):
    try:
        state=json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError,UnicodeError) as error:
        raise ValueError('invalid state') from error
    if not isinstance(state,dict) or state.get('version')!=1 or type(state.get('version')) is not int or not isinstance(state.get('tickets'),dict) or not isinstance(state.get('batches'),list) or not isinstance(state.get('audit'),list):
        raise ValueError('invalid state')
    return state

def save(state,path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,name=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as file:
            json.dump(state,file,ensure_ascii=False,sort_keys=True)
            file.flush(); os.fsync(file.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)
''',
"report.py": '''def summarize(state):
    status={'open':0,'done':0}; priority={'low':0,'high':0}
    for item in state['tickets'].values():
        status[item['status']]+=1; priority[item['priority']]+=1
    return {'total':len(state['tickets']),'status':status,'priority':priority}
''',
"cli.py": '''import argparse
import json
from . import persistence, service, report

def main(argv=None):
    parser=argparse.ArgumentParser(); parser.add_argument('database'); sub=parser.add_subparsers(dest='command',required=True)
    for name in ('import','transition'):
        p=sub.add_parser(name); p.add_argument('input'); p.add_argument('batch_id')
    sub.add_parser('report'); args=parser.parse_args(argv)
    try:
        state=persistence.load(args.database)
        if args.command=='report':
            result=report.summarize(state)
        else:
            rows=[]
            if args.batch_id not in state['batches']:
                with open(args.input,encoding='utf-8') as file: rows=json.load(file)
            function=service.apply_batch if args.command=='import' else service.transition_batch
            result=function(state,rows,args.batch_id)
            if args.batch_id not in state['batches']: persistence.save(result,args.database)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return 0
    except (ValueError,OSError) as error:
        import sys
        print(str(error),file=sys.stderr); return 2

if __name__=='__main__':
    raise SystemExit(main())
'''
}

PUBLIC = '''import sys
from tickets.normalize import normalize
from tickets.service import apply_batch
from tickets.report import summarize

state={'version':1,'tickets':{},'batches':[],'audit':[]}
row={'id':' A ','title':'One','status':'open','priority':'high'}
assert normalize(row)['id']=='A' and row['id']==' A '
updated=apply_batch(state,[row],'batch')
assert not state['tickets'] and updated['tickets']['A']['revision']==1
assert apply_batch(updated,[row],'batch')==updated
if '--base' not in sys.argv:
    assert summarize(updated)=={'total':1,'status':{'open':1,'done':0},'priority':{'low':0,'high':1}}
print('public pass')
'''

PUBLIC_CHANGE = '''from tickets.service import transition_batch
state={'version':1,'tickets':{'A':{'id':'A','title':'One','status':'open','priority':'high','revision':1}},'batches':[],'audit':[]}
new=transition_batch(state,[{'id':'A','expected_revision':1,'status':'done'}],'change')
assert new['tickets']['A']['revision']==2 and state['tickets']['A']['status']=='open'
try:
    transition_batch(new,[{'id':'A','expected_revision':1,'status':'open'}],'conflict')
except ValueError:
    pass
else:
    raise AssertionError('missing conflict detection')
print('change pass')
'''

HIDDEN = '''import copy,json,tempfile,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import os
sys.path.insert(0, os.getcwd())
from tickets.normalize import normalize
from tickets.service import apply_batch,transition_batch
from tickets.persistence import save,load
from tickets.report import summarize

def empty(): return {'version':1,'tickets':{},'batches':[],'audit':[]}
def row(ident='A'): return {'id':ident,'title':'一','status':'open','priority':'high'}
def rejects(function,*args):
    try: function(*args)
    except ValueError: return
    raise AssertionError('must reject invalid value')

checks=[]
def check(name,function):
    try: function(); checks.append({'id':name,'passed':True})
    except Exception as error: checks.append({'id':name,'passed':False,'error':type(error).__name__+':'+str(error)})

def normalization():
    r=row(' A '); old=copy.deepcopy(r); n=normalize(r)
    assert n['id']=='A' and r==old
    for bad in [None,{'id':True},{**row(),'status':'other'},{**row(),'priority':'urgent'},{**row(),'unexpected':'x'}]: rejects(normalize,bad)
check('normalization',normalization)

def atomic_import():
    s=empty(); previous=copy.deepcopy(s)
    rejects(apply_batch,s,[row(),{**row('B'),'status':'bad'}],'a'); assert s==previous
    rejects(apply_batch,s,[row(),row()],'b'); assert s==previous
    n=apply_batch(s,[row()],'ok'); assert s==previous and n['tickets']['A']['revision']==1
    n['tickets']['A']['title']='changed'; assert not s['tickets']
check('atomic_import',atomic_import)

def preserve_blanks():
    s=apply_batch(empty(),[row()],'first'); n=apply_batch(s,[{'id':'A','title':'','priority':'low'}],'second')
    assert n['tickets']['A']['title']=='一' and n['tickets']['A']['status']=='open' and n['tickets']['A']['priority']=='low' and n['tickets']['A']['revision']==2
    rejects(apply_batch,empty(),[{'id':'new'}],'invalid')
check('preserve_blank_updates',preserve_blanks)

def idempotency_audit():
    s=apply_batch(empty(),[row('Z'),row('A')],'a'); again=apply_batch(s,None,'a')
    assert again==s and again is not s and again['tickets'] is not s['tickets']
    assert s['audit']==[{'batch_id':'a','kind':'import','ids':['A','Z']}]
    rejects(apply_batch,s,[],' ')
check('idempotency_audit',idempotency_audit)

def transitions():
    s=apply_batch(empty(),[row('A'),row('B')],'import'); original=copy.deepcopy(s)
    good={'id':'A','expected_revision':1,'status':'done'}
    rejects(transition_batch,s,[good,{'id':'B','expected_revision':2,'status':'done'}],'conflict'); assert s==original
    rejects(transition_batch,s,[good,good],'duplicate')
    rejects(transition_batch,s,[{**good,'expected_revision':True}],'bool')
    rejects(transition_batch,s,[{**good,'id':'missing'}],'missing')
    rejects(transition_batch,s,[{**good,'status':'other'}],'bad')
    n=transition_batch(s,[good],'change'); assert n['tickets']['A']['revision']==2 and n['tickets']['A']['status']=='done' and s==original
    assert transition_batch(n,None,'change')==n
    assert n['audit'][-1]=={'batch_id':'change','kind':'transition','ids':['A']}
check('atomic_optimistic_transition',transitions)

def persistence():
    s=apply_batch(empty(),[row()],'x')
    with tempfile.TemporaryDirectory() as directory:
        p=Path(directory)/'nested/state.json'; save(s,p); assert load(p)==s
        before=p.read_bytes()
        with patch('os.replace',side_effect=OSError('synthetic replacement failure')):
            try: save(empty(),p)
            except OSError: pass
        assert p.read_bytes()==before and list(p.parent.iterdir())==[p]
        p.write_text('{bad'); rejects(load,p)
        p.write_text(json.dumps({'version':True,'tickets':{},'batches':[],'audit':[]})); rejects(load,p)
check('persistence_atomic_and_invalid',persistence)

def reports():
    s=apply_batch(empty(),[row('A'),{**row('B'),'status':'done','priority':'low'}],'x'); previous=copy.deepcopy(s)
    assert summarize(s)=={'total':2,'status':{'open':1,'done':1},'priority':{'low':1,'high':1}} and s==previous
    assert summarize(empty())['total']==0
check('report_no_mutation',reports)

def cli():
    with tempfile.TemporaryDirectory() as directory:
        p=Path(directory)/'db.json'; data=Path(directory)/'rows.json'; changes=Path(directory)/'changes.json';save(empty(),p);data.write_text(json.dumps([row()]))
        result=subprocess.run([sys.executable,'-m','tickets.cli',str(p),'import',str(data),'a'],capture_output=True,text=True)
        assert result.returncode==0,('CLI import must read a JSON FILE PATH',result.returncode,result.stderr)
        assert json.loads(result.stdout)['tickets']['A']['revision']==1
        changes.write_text(json.dumps([{'id':'A','expected_revision':1,'status':'done'}]))
        result=subprocess.run([sys.executable,'-m','tickets.cli',str(p),'transition',str(changes),'b'],capture_output=True,text=True)
        assert result.returncode==0,('CLI transition must read a JSON FILE PATH',result.returncode,result.stderr)
        replay_before=p.read_bytes(); replay_identity=(p.stat().st_ino,p.stat().st_mtime_ns); changes.write_text('{bad')
        replay=subprocess.run([sys.executable,'-m','tickets.cli',str(p),'transition',str(changes),'b'],capture_output=True,text=True)
        assert replay.returncode==0 and p.read_bytes()==replay_before and (p.stat().st_ino,p.stat().st_mtime_ns)==replay_identity,('repeat batch must ignore replacement payload',replay.stderr)
        changes.write_text(json.dumps([{'id':'A','expected_revision':1,'status':'done'}]))
        before=p.read_bytes(); result=subprocess.run([sys.executable,'-m','tickets.cli',str(p),'transition',str(changes),'c'],capture_output=True,text=True)
        assert result.returncode==2 and p.read_bytes()==before
        result=subprocess.run([sys.executable,'-m','tickets.cli',str(p),'report'],capture_output=True,text=True)
        assert result.returncode==0 and json.loads(result.stdout)['status']['done']==1
check('cli_import_transition_errors',cli)
print(json.dumps({'passed':all(c['passed'] for c in checks),'checks':checks},ensure_ascii=False))
raise SystemExit(0 if all(c['passed'] for c in checks) else 1)
'''

DESCRIPTION = '''# 工单批量导入和状态报表

在 tickets 项目中完成输入规范化、原子批量导入、保存读取、状态统计和 CLI。只使用 Python 标准库。

状态结构为 {version:1,tickets:{id:工单},batches:[批次ID],audit:[记录]}。工单包含 id/title/status/priority/revision。
normalize 接受字典，只允许 id/title/status/priority；四字段必须是字符串，缺省为""，去除首尾空格。
id 非空；status 只允许空/open/done；priority 只允许空/low/high。返回新字典，不修改输入。
apply_batch(state,rows,batch_id) 返回深复制的新状态。新工单四字段都非空，revision=1；更新时空字段保留旧值，revision加1。
输入行非法或规范化后ID重复时整批失败、输入状态不变。batch_id 为非空白字符串，按原字符串识别；已经执行的批次直接返回状态副本，不再校验新载荷、不重复修改或追加审计。
成功导入追加一条 {batch_id,kind:"import",ids:排序后的ID数组}。空批次允许，仍记录批次和审计。
save 使用 UTF-8 JSON，创建父目录，同目录临时文件原子替换；失败保留旧文件并清理临时文件。
load 对损坏 JSON、顶层不是字典、version 非整数1、tickets不是字典、batches/audit不是数组抛 ValueError；本期不要求完整校验已有工单的所有字段。
summarize 返回 {total,status:{open,done},priority:{low,high}}，包含零项计数，不修改输入。
CLI: python -m tickets.cli DATABASE import ROWS_JSON_PATH BATCH_ID，以及 report。
DATABASE 是已存在且格式正确的数据库 JSON 文件路径；ROWS_JSON_PATH 是 UTF-8 JSON 文件路径，文件中存放行数组，不是直接传入的 JSON 字符串。
CLI 必须打开载荷文件后读取 JSON；不得将文件路径本身交给 json.loads。新的批次遇到文件不存在、坏JSON或非法数据，正常错误处理为 stderr/退出2且数据库字节不变。
已执行批次按函数幂等规则直接返回已有状态，不读取新载荷文件、不重新写数据库。
正常 stdout 恰为一行 JSON/退出0；错误 stderr/退出2且数据库不变。
例如已有数据库 .tmp/db.json 和行数组文件 .tmp/rows.json 时，命令为：python -m tickets.cli .tmp/db.json import .tmp/rows.json batch-1。
公开 public_cli_check.py 会核对文件路径输入、报表、错误退出和数据库不变。

可修改 tickets/、tests/、README.md。公开检查 public_check.py/public_change_check.py/public_cli_check.py/public_cli_change_check.py 不可修改。数据库必须写到 .tmp/ 或临时目录。
任务中会在基础导入业务探针首次通过后追加一项需求，四组条件相同。请保留既有接口和已完成代码。
'''

CHANGE = '''新增 transition_batch(state,changes,batch_id) 和 CLI transition CHANGES_JSON_PATH BATCH_ID。
CHANGES_JSON_PATH 是 UTF-8 JSON 文件路径，文件中是更改数组，不是内联 JSON；新的批次必须打开文件读取，已执行批次不读取新载荷。
changes 为字典数组，每项恰含 id/expected_revision/status；id 必须是已有工单的精确ID，不能重复。
expected_revision 必须是整数且不能是bool，必须与原状态版本相同；status 为 open/done。
所有更改先校验再执行；一项非法则整批失败、原状态不变。成功后每项 revision 加1，追加批次和 kind:"transition" 的排序审计。
批次幂等规则与导入一致，批次ID空间共用；已执行批次直接返回深复制，不校验新载荷。空更改数组允许。
CLI 使用相同成功和错误约定。保留导入、保存读取和报表全部行为。'''


# The same CLI contract is exercised publicly and privately, with distinct cases.
PUBLIC_CLI = '''import json,subprocess,sys,tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory); db=root/'db.json'; rows=root/'rows.json'
    db.write_text(json.dumps({'version':1,'tickets':{},'batches':[],'audit':[]}))
    rows.write_text(json.dumps([{'id':'A','title':'公开工单','status':'open','priority':'high'}]))
    def invoke(*args):
        return subprocess.run([sys.executable,'-m','tickets.cli',str(db),*args],capture_output=True,text=True)
    result=invoke('import',str(rows),'public-import')
    assert result.returncode==0,('import 参数必须是 JSON 文件路径',result.returncode,result.stderr)
    assert len(result.stdout.splitlines())==1 and json.loads(result.stdout)['tickets']['A']['revision']==1
    before=db.read_bytes(); identity=(db.stat().st_ino,db.stat().st_mtime_ns); rows.write_text('{invalid')
    replay=invoke('import',str(rows),'public-import')
    assert replay.returncode==0 and db.read_bytes()==before and (db.stat().st_ino,db.stat().st_mtime_ns)==identity,('重复批次不读取新载荷',replay.stderr)
    failed=invoke('import',str(rows),'invalid-new-batch')
    assert failed.returncode==2 and failed.stderr and not failed.stdout and db.read_bytes()==before
    report=invoke('report')
    assert report.returncode==0 and json.loads(report.stdout)['total']==1
print('PUBLIC_CLI_PASSED')
'''

PUBLIC_CLI_CHANGE = '''import json,subprocess,sys,tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory); db=root/'db.json'; changes=root/'changes.json'
    db.write_text(json.dumps({'version':1,'tickets':{'A':{'id':'A','title':'追加工单','status':'open','priority':'high','revision':1}},'batches':[],'audit':[]}))
    changes.write_text(json.dumps([{'id':'A','expected_revision':1,'status':'done'}]))
    def invoke(batch):
        return subprocess.run([sys.executable,'-m','tickets.cli',str(db),'transition',str(changes),batch],capture_output=True,text=True)
    result=invoke('public-change')
    assert result.returncode==0,('transition 参数必须是 JSON 文件路径',result.returncode,result.stderr)
    assert len(result.stdout.splitlines())==1 and json.loads(result.stdout)['tickets']['A']['revision']==2
    before=db.read_bytes(); identity=(db.stat().st_ino,db.stat().st_mtime_ns); stale=invoke('public-conflict')
    assert stale.returncode==2 and stale.stderr and not stale.stdout and db.read_bytes()==before
    changes.unlink(); replay=invoke('public-change')
    assert replay.returncode==0 and db.read_bytes()==before and (db.stat().st_ino,db.stat().st_mtime_ns)==identity,('重复批次不读取新载荷文件',replay.stderr)
print('PUBLIC_CLI_CHANGE_PASSED')
'''


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def prepare(package=PACKAGE):
    package=inside_project(package)
    if package.exists():
        raise SystemExit("候选题目录已存在，禁止覆盖；修改应另建版本。")
    for name, source in MODULES.items():
        write(package / "initial/tickets" / name, source)
    write(package / "initial/tickets/__init__.py", "")
    write(package / "initial/README.md", DESCRIPTION)
    write(package / "initial/public_check.py", PUBLIC)
    write(package / "initial/public_change_check.py", PUBLIC_CHANGE)
    write(package / "initial/public_cli_check.py", PUBLIC_CLI)
    write(package / "initial/public_cli_change_check.py", PUBLIC_CLI_CHANGE)
    for name, source in REFERENCE.items():
        write(package / "reference/tickets" / name, source)
    write(package / "reference/tickets/__init__.py", "")
    write(package / "grade.py", HIDDEN)
    python = str(ROOT / ".venv/bin/python")
    task = {"id":"C1-v2","objective":"完成工单批量导入、持久化、状态报表和CLI，补充项目测试与运行说明。",
            "constraints":["只用Python标准库，不访问公网，不修改公开检查。", "保持README里的函数和CLI契约；载荷参数是JSON文件路径，不是内联JSON。"],
            "acceptance":["输入校验、批次幂等、原子导入、空值保留、持久化、报表和CLI正确。"],
            "write_paths":["tickets/","tests/","README.md"],"protected_paths":["public_check.py","public_change_check.py","public_cli_check.py","public_cli_change_check.py"],
            "checks":[{"id":"public_initial","argv":[python,"public_check.py"],"timeout":30},{"id":"public_cli_initial","argv":[python,"public_cli_check.py"],"timeout":15}],
            "feedback":{"probe":{"id":"business_import","argv":[python,"public_check.py","--base"],"timeout":15},
                "text":CHANGE,"acceptance":["乐观版本核对、原子状态变更、幂等和错误CLI全部正确。"],
                "checks":[{"id":"public_change","argv":[python,"public_change_check.py"],"timeout":30},{"id":"public_cli_change","argv":[python,"public_cli_change_check.py"],"timeout":15}]}}
    Task.model_validate(task)
    write(package / "contract.json", json.dumps({"version":"C1-v2","cli_payload":"utf8_json_file_path","replay":"skip_payload_read_and_database_write","protected_cli_checks":["public_cli_check.py","public_cli_change_check.py"]},ensure_ascii=False,indent=2))
    write(package / "task.json", json.dumps(task,ensure_ascii=False,indent=2))
    write(package / "description.md", DESCRIPTION+"\n\n## 追加要求与触发条件\n\n"+CHANGE+"\n\n框架在工具事务结束后检查基础导入探针。首次通过就向各组同一会话送达相同追加要求；不以请求数、时间或强制压缩触发。\n\n难度为待实测校准的中等候选：5个相互调用的模块，要求输入隔离、批次原子性、幂等、文件原子替换和CLI一致性。它不保证产生多次压缩，因此本题只诊断编码交付效率。\n")
    return package


if __name__ == "__main__":
    print("Prepared offline; no model calls:", prepare())
