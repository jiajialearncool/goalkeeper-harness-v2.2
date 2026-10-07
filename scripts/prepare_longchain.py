"""Fresh long-chain task. Reference code is authored here, never a previous model answer."""
import json
import textwrap
from goalkeeper.paths import PROJECT, inside_project
from goalkeeper.models import Task

PACKAGE=PROJECT/'evaluation/private/L1-v1'
DOMAIN='''from copy import deepcopy

STATUSES=('open','active','done')
PRIORITIES=('low','normal','high')

def text(value,blank=False):
    if not isinstance(value,str) or (not blank and not value.strip()): raise ValueError('text')
    return value.strip()

def integer(value,minimum=0):
    if type(value) is not int or value<minimum: raise ValueError('integer')
    return value

def ticket(row):
    if not isinstance(row,dict) or set(row)-{'id','title','status','priority','owner','tags','revision'}: raise ValueError('ticket fields')
    ident=text(row.get('id')); title=text(row.get('title'))
    status=row.get('status','open'); priority=row.get('priority','normal')
    if status not in STATUSES or priority not in PRIORITIES: raise ValueError('enum')
    owner=text(row.get('owner',''),True); tags=row.get('tags',[])
    if not isinstance(tags,list): raise ValueError('tags')
    tags=sorted(set(text(tag) for tag in tags))
    return dict(id=ident,title=title,status=status,priority=priority,owner=owner,tags=tags,revision=integer(row.get('revision',1),1))

def validate(state):
    if not isinstance(state,dict) or set(state)!={'schema','revision','tickets','batches','audit','undo'} or type(state['schema']) is not int or state['schema']!=2: raise ValueError('state')
    integer(state['revision'])
    if not isinstance(state['tickets'],dict): raise ValueError('tickets')
    for key,row in state['tickets'].items():
        if ticket(row)!=row or key!=row['id']: raise ValueError('canonical ticket')
    if not isinstance(state['batches'],list) or any(text(b)!=b for b in state['batches']) or len(set(state['batches']))!=len(state['batches']): raise ValueError('batches')
    for name in ('audit','undo'):
        if not isinstance(state[name],list) or any(not isinstance(v,dict) for v in state[name]): raise ValueError(name)
    return deepcopy(state)

def migrate(raw):
    if not isinstance(raw,dict) or type(raw.get('schema')) is not int: raise ValueError('schema')
    if raw['schema']==2: return validate(raw)
    if raw['schema'] not in (0,1) or set(raw)!={'schema','tickets'}: raise ValueError('legacy fields')
    rows=raw['tickets']; out=dict(schema=2,revision=0,tickets={},batches=[],audit=[],undo=[])
    if raw['schema']==0:
        if not isinstance(rows,list): raise ValueError('old rows')
        converted=[]
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'key','text','closed','level'} or type(row['closed']) is not bool or type(row['level']) is not int or row['level'] not in (1,2): raise ValueError('old row')
            converted.append(dict(id=row['key'],title=row['text'],status='done' if row['closed'] else 'open',priority='high' if row['level']==2 else 'low'))
        rows=converted
    else:
        if not isinstance(rows,dict): raise ValueError('v1 rows')
        converted=[]
        for key,row in rows.items():
            if not isinstance(row,dict) or set(row)!={'id','title','status','priority'} or key!=row['id'] or row['status'] not in ('open','done') or row['priority'] not in ('low','high'): raise ValueError('v1 row')
            converted.append(row)
        rows=converted
    for row in rows:
        item=ticket(row)
        if item['id'] in out['tickets']: raise ValueError('duplicate normalized id')
        out['tickets'][item['id']]=item
    return out

def plan_result(state,plan):
    original=validate(state)
    if not isinstance(plan,dict): raise ValueError('plan')
    batch=text(plan.get('batch_id'))
    if batch!=plan['batch_id']: raise ValueError('batch id canonical')
    if batch in original['batches']:
        return original,dict(batch_id=batch,base_revision=original['revision'],next_revision=original['revision'],changed_ids=[],operations_count=0,tickets=len(original['tickets']),replayed=True)
    if set(plan)!={'batch_id','expected_revision','operations'} or integer(plan['expected_revision'])!=original['revision']: raise ValueError('state conflict')
    operations=plan['operations']
    if not isinstance(operations,list): raise ValueError('operations')
    out=deepcopy(original); changed=set()
    for operation in operations:
        if not isinstance(operation,dict): raise ValueError('operation')
        kind=operation.get('kind'); ident=text(operation.get('id'))
        if ident!=operation['id']: raise ValueError('operation id canonical')
        if kind=='create':
            if set(operation)-{'kind','id','title','status','priority','owner','tags'} or ident in out['tickets']: raise ValueError('create')
            item=ticket({k:v for k,v in operation.items() if k!='kind'});out['tickets'][ident]=item
        else:
            required={'kind','id','expected_revision'}|({'changes'} if kind=='update' else {'status'} if kind=='transition' else {'owner'} if kind=='assign' else set())
            if kind not in ('update','transition','assign','delete') or set(operation)!=required or ident not in out['tickets']: raise ValueError('operation fields')
            item=out['tickets'][ident]
            if integer(operation['expected_revision'],1)!=item['revision']: raise ValueError('ticket conflict')
            if kind=='delete': del out['tickets'][ident]
            else:
                patch=operation['changes'] if kind=='update' else {('status' if kind=='transition' else 'owner'):operation['status' if kind=='transition' else 'owner']}
                if not isinstance(patch,dict) or not patch or kind=='update' and set(patch)-{'title','priority','owner','tags'}: raise ValueError('patch')
                out['tickets'][ident]=ticket({**item,**patch,'revision':item['revision']+1})
        changed.add(ident)
    ids=sorted(changed);out['revision']+=1;out['batches'].append(batch)
    out['audit'].append(dict(revision=out['revision'],batch_id=batch,kind='apply',changed_ids=ids))
    out['undo'].append(dict(batch_id=batch,before=deepcopy(original['tickets']),changed_ids=ids))
    return out,dict(batch_id=batch,base_revision=original['revision'],next_revision=out['revision'],changed_ids=ids,operations_count=len(operations),tickets=len(out['tickets']),replayed=False)

def preview(state,plan): return plan_result(state,plan)[1]
def apply_plan(state,plan): return plan_result(state,plan)[0]

def select(state,query):
    data=validate(state)
    if not isinstance(query,dict) or set(query)-{'status','priority','owner','tags_all','sort','offset','limit'}: raise ValueError('query')
    if 'status' in query and query['status'] not in STATUSES or 'priority' in query and query['priority'] not in PRIORITIES: raise ValueError('filter enum')
    if 'owner' in query and text(query['owner'],True)!=query['owner']: raise ValueError('owner')
    tags=query.get('tags_all',[])
    if not isinstance(tags,list): raise ValueError('tags filter')
    tags=set(text(t) for t in tags)
    sorting=query.get('sort','id')
    if sorting not in ('id','priority'): raise ValueError('sort')
    offset=integer(query.get('offset',0));limit=query.get('limit')
    if limit is not None: integer(limit)
    rows=[deepcopy(row) for row in data['tickets'].values() if all(row[k]==query[k] for k in ('status','priority','owner') if k in query) and tags.issubset(row['tags'])]
    rows.sort(key=lambda row: row['id'] if sorting=='id' else (({'high':0,'normal':1,'low':2})[row['priority']],row['id']))
    return rows[offset:] if limit is None else rows[offset:offset+limit]

def stats(state,query):
    rows=select(state,query);status={k:0 for k in STATUSES};priority={k:0 for k in PRIORITIES};owners={};tags={}
    for row in rows:
        status[row['status']]+=1;priority[row['priority']]+=1;owners[row['owner']]=owners.get(row['owner'],0)+1
        for tag in row['tags']: tags[tag]=tags.get(tag,0)+1
    return dict(total=len(rows),status=status,priority=priority,owners=dict(sorted(owners.items())),tags=dict(sorted(tags.items())))

def changes_since(state,since):
    data=validate(state);integer(since)
    return deepcopy([event for event in data['audit'] if event['revision']>since])

def rollback(state,source_batch_id,rollback_batch_id,expected_revision):
    out=validate(state);source=text(source_batch_id);batch=text(rollback_batch_id)
    if source!=source_batch_id or batch!=rollback_batch_id: raise ValueError('batch id canonical')
    if batch in out['batches']: return out
    if integer(expected_revision)!=out['revision'] or not out['undo'] or out['undo'][-1]['batch_id']!=source: raise ValueError('rollback conflict')
    frame=out['undo'].pop()
    for ident in frame['changed_ids']:
        if ident not in frame['before']: out['tickets'].pop(ident,None)
        else:
            row=deepcopy(frame['before'][ident]);current=out['tickets'].get(ident,{'revision':0})
            row['revision']=max(row['revision'],current['revision'])+1;out['tickets'][ident]=row
    out['revision']+=1;out['batches'].append(batch)
    out['audit'].append(dict(revision=out['revision'],batch_id=batch,kind='rollback',source_batch_id=source,changed_ids=frame['changed_ids']))
    return out
'''
COMPAT='''from copy import deepcopy

def add(state,row):
    if not isinstance(state,dict) or state.get('schema')!=1 or not isinstance(state.get('tickets'),dict): raise ValueError('legacy state')
    if not isinstance(row,dict) or set(row)!={'id','title','status','priority'} or any(not isinstance(v,str) for v in row.values()): raise ValueError('legacy row')
    row={k:v.strip() for k,v in row.items()}
    if not row['id'] or not row['title'] or row['id'] in state['tickets'] or row['status'] not in ('open','done') or row['priority'] not in ('low','high'): raise ValueError('legacy values')
    out=deepcopy(state);out['tickets'][row['id']]=row;return out

def summary(state):
    rows=state['tickets'].values()
    return {'total':len(state['tickets']),'open':sum(r['status']!='done' for r in rows),'done':sum(r['status']=='done' for r in state['tickets'].values())}
'''
REPOSITORY='''import json,os,tempfile
from pathlib import Path
from .migration import migrate
from .schema import validate

def load(path):
    try:
        raw=json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError,UnicodeError) as error: raise ValueError('invalid JSON') from error
    return migrate(raw)

def save(state,path):
    value=validate(state);path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
'''
CLI='''import argparse,json,sys
from . import repository,planner,queries,audit,rollback,compat

def read(path):
    with open(path,encoding='utf-8') as f:return json.load(f)

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('database');sub=parser.add_subparsers(dest='command',required=True)
    for name in ('add','preview','apply','query','stats'):
        sub.add_parser(name).add_argument('input')
    sub.add_parser('migrate');sub.add_parser('report')
    sub.add_parser('audit').add_argument('since',type=int)
    rb=sub.add_parser('rollback');rb.add_argument('source');rb.add_argument('batch');rb.add_argument('revision',type=int)
    args=parser.parse_args(argv)
    try:
        state=repository.load(args.database)
        if args.command=='migrate':repository.save(state,args.database);result=state
        elif args.command=='report':result=compat.summary(state)
        elif args.command=='add':
            row=read(args.input)
            # Validate the original four-field legacy interface before v2 planning.
            checked=compat.add({'schema':1,'tickets':{}},row)['tickets']
            row=next(iter(checked.values()))
            plan={'batch_id':f"legacy-add:{state['revision']}:{row['id']}",'expected_revision':state['revision'],'operations':[{'kind':'create',**row}]}
            result=planner.apply_plan(state,plan);repository.save(result,args.database)
        elif args.command in ('preview','apply'):
            plan=read(args.input)
            if args.command=='preview':result=planner.preview(state,plan)
            else:
                result=planner.apply_plan(state,plan)
                if plan['batch_id'] not in state['batches']:repository.save(result,args.database)
        elif args.command in ('query','stats'):result=getattr(queries,'select' if args.command=='query' else 'stats')(state,read(args.input))
        elif args.command=='audit':result=audit.changes_since(state,args.since)
        else:
            result=rollback.rollback(state,args.source,args.batch,args.revision)
            if args.batch not in state['batches']:repository.save(result,args.database)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (ValueError,OSError) as error:
        print(str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
'''
INITIAL_REPOSITORY='''import json
from pathlib import Path

def load(path):
    state=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(state,dict) or state.get('schema')!=1 or not isinstance(state.get('tickets'),dict):raise ValueError('legacy state')
    return state

def save(state,path):
    Path(path).write_text(json.dumps(state,ensure_ascii=False,sort_keys=True),encoding='utf-8')
'''
INITIAL_CLI='''import argparse,json,sys
from . import compat,repository

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('database');sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('add').add_argument('input');sub.add_parser('report');args=parser.parse_args(argv)
    try:
        state=repository.load(args.database)
        if args.command=='add':
            with open(args.input,encoding='utf-8') as f:row=json.load(f)
            state=compat.add(state,row);repository.save(state,args.database);result=state
        else:result=compat.summary(state)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (ValueError,OSError) as error:print(str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
'''

DESCRIPTION='''# L1-v1：升级已有工单项目，完成迁移、批量执行与可追溯报表

这是已有 board 项目的持续改造任务。已有 compat.add/compat.summary 以及 CLI add/report 能运行，并有 tests/test_legacy.py。保留这些接口与行为，只用 Python 标准库。完成以下相互依赖的改造、项目测试和 README 运行说明，不访问公网。项目测试使用标准库 unittest，至少3项可运行测试；README须有 python -m board.cli 的 preview/apply/rollback 使用示例和 unittest 测试命令。

## 数据格式与迁移
board.migration.migrate(raw) 返回新的规范 schema=2 状态，绝不修改输入。schema=0/1 迁移后 revision=0、batches/audit/undo 均为空。
- schema=0 顶层恰为 {schema:0,tickets:数组}；每行恰为 {key,text,closed,level}。key/text 是非空白字符串，去首尾空格；closed 必须是 bool；level 必须是整数1或2，不能是bool。closed true映射done，否则open；level 2映射high，否则low。
- schema=1 顶层恰为 {schema:1,tickets:字典}。每行恰含 id/title/status/priority，字典键必须与原 id 相同；字符串 id/title 去空格且非空，status 为open/done，priority 为low/high。迁移后规范化ID不可重复。
- schema=2 顶层恰含 schema/revision/tickets/batches/audit/undo。schema 必须是整数2，revision 是非负整数且不能是bool；tickets为字典，键是规范化ID；batches为不重复的非空白且无首尾空格字符串数组；audit/undo 为对象数组，本期不要求校验历史对象内部的每个字段。
工单恰含 id/title/status/priority/owner/tags/revision。id/title 是去空格后的非空字符串；status为open/active/done，priority为low/normal/high；owner为去空格后的字符串，允许空；tags为非空白字符串数组，去空格、去重并按字符串升序排序；工单revision为正整数、不能bool。旧数据迁移默认owner=""、tags=[]、工单revision=1。schema=2 已规范状态返回深复制，非法或非规范数据抛 ValueError，不偷偷修复。
board.schema.ticket(row) 接受只含上述工单字段的字典，id/title必填，其余默认open/normal/""/[]/1，返回规范化的新工单。validate(state) 按 schema=2 规则校验并返回深复制。任一非法记录使整次迁移失败；migrate(migrate(raw)) 与一次迁移结果相同。

## 批量变更：先预览，再整体执行
board.planner.preview(state,plan) 返回预览对象、不改变状态；apply_plan(state,plan) 返回新的深复制状态。plan 恰含 batch_id/expected_revision/operations；batch_id 为非空白且无首尾空格字符串；expected_revision 是非负整数、不能bool，必须等于数据库revision；operations为数组，可为空。
操作按给定顺序执行，同一ID可以多次出现，每步以此前步骤的临时结果为依据：
- create：kind/id/title 必填；可选 status/priority/owner/tags，默认与ticket相同；不允许其他字段，ID必须尚不存在，工单revision=1。
- update：恰含 kind/id/expected_revision/changes；changes是非空字典，只可改title/priority/owner/tags；用ticket规则校验结果，不可直接改status。
- transition：恰含 kind/id/expected_revision/status，状态取open/active/done，三者之间都允许转移。
- assign：恰含 kind/id/expected_revision/owner，可用空字符串取消分配。
- delete：恰含 kind/id/expected_revision。
以上操作ID必须是精确规范ID；除create外，ID须已存在，expected_revision为正整数且等于当前步骤工单revision。成功的update/transition/assign把工单revision加1，delete移除工单。任一步非法或版本冲突，全批失败、输入不变。
成功批次把数据库revision加1，向batches追加batch_id，audit追加 {revision:新数据库版本,batch_id,kind:"apply",changed_ids:排序去重的涉及ID数组}，并向undo追加 {batch_id,before:执行前tickets深复制,changed_ids}。空批次也记录版本、审计与undo。
重复batch_id先直接返回当前状态副本，不校验新的expected_revision/operations，也不新增审计或版本；至少要求plan为对象且batch_id有效可识别。
preview结果恰为 {batch_id,base_revision,next_revision,changed_ids,operations_count,tickets,replayed}。首次预览next_revision=base_revision+1，tickets为执行后的工单数；重复批次next_revision=base_revision，changed_ids=[]，operations_count=0，replayed=true。

## 查询、统计和审计必须使用执行后的同一状态
board.queries.select(state,query) 返回深复制工单列表，query只允许 status/priority/owner/tags_all/sort/offset/limit。前三项为精确值筛选，tags_all为标签数组且要求全部匹配；空标签筛选不限制。sort默认id升序，priority排序为high/normal/low，同优先级按id升序。offset默认0，为非负整数；limit默认null不限，若指定为非负整数，0返回空列表；两者不能bool。未知字段、错误枚举、非规范owner或非法标签/分页参数抛ValueError。
board.queries.stats(state,query) 对select返回的同一组（包含分页）统计，返回 {total,status:{open,active,done},priority:{low,normal,high},owners:各owner计数,tags:各tag计数}；状态和优先级保留零项，空owner直接用""作为键，owners/tags键升序。不修改输入。
board.audit.changes_since(state,since) 返回revision严格大于since的审计对象深复制，保持原顺序；since为非负整数不能bool。

## 保存、CLI与兼容性
repository.load(path) 从UTF-8 JSON读取并调用migrate，返回schema=2但不改写原文件。repository.save(state,path) 先验证规范schema=2，用同目录临时文件原子替换、创建父目录；任何失败保留旧文件并清理临时文件。
CLI 全部格式为 python -m board.cli DATABASE COMMAND ...，DATABASE 是已有数据库文件，所有 INPUT/PLAN/QUERY 参数都是UTF-8 JSON文件路径，不能解释为内联JSON：
add INPUT_PATH；report；migrate；preview PLAN_PATH；apply PLAN_PATH；query QUERY_PATH；stats QUERY_PATH；audit SINCE_REVISION。
add仍接收旧版四字段对象并拒绝重复ID，新系统用create执行，自动batch_id为 legacy-add:{当前数据库revision}:{规范ID}。report保持旧版恰有 total/open/done 三个字段，active也计入open。compat.add(state,row) 保持schema=1旧函数契约，compat.summary保留旧版统计接口。
正常stdout恰一行JSON、退出0；错误stderr且stdout为空、退出2。错误、preview/query/stats/audit/report不改写数据库；重复apply批次也不重写数据库。apply需要先读取JSON文件才能识别其中batch_id，文件本身损坏不能当作重复批次忽略。migrate持久保存迁移结果。所有数据库写在.tmp/或系统临时目录。
可修改board/、tests/、README.md；public_legacy.py/public_initial.py/public_change.py为受保护规定检查，不能修改。隐藏评分不在任务目录。基础迁移与计划预览探针首次通过后，会在同一会话追加一次需求；请继续完成并保留此前行为。
'''
CHANGE='''新增“撤销最近一次仍可撤销批次”的功能，必须在迁移、计划执行、版本检查、持久化、查询报表和审计之间保持一致。
board.rollback.rollback(state,source_batch_id,rollback_batch_id,expected_revision) 返回深复制新状态。两个ID是非空白且无首尾空格字符串。若rollback_batch_id已经在batches中，直接返回副本，忽略新的来源是否存在或expected_revision是否冲突。否则expected_revision为非负整数、不能bool、等于数据库revision；source_batch_id必须等于undo栈最后一项的batch_id，否则抛ValueError且输入不变。
弹出该undo项，只恢复changed_ids内的工单：执行前不存在的ID移除；执行前存在的ID恢复内容，工单revision设为max(执行前revision,当前revision或0)+1；其他ID的内容和revision保持现状。这使多次依次撤销也不会把无关工单版本倒退。
数据库revision仍加1，不恢复旧的全局版本或batches；保留原批次在batches，追加rollback_batch_id，使被撤销原批次再次apply仍幂等。audit追加 {revision:新数据库版本,batch_id:rollback_batch_id,kind:"rollback",source_batch_id,changed_ids:原undo项的数组}。撤销本身不再追加undo项，可再撤销栈中更早的批次。旧预览和旧工单版本此后须正确冲突。
新增 CLI：python -m board.cli DATABASE rollback SOURCE_BATCH_ID ROLLBACK_BATCH_ID EXPECTED_REVISION，按相同JSON成功/错误约定；失败或重复撤销不改写数据库。撤销后query/stats/audit/report和所有原功能必须正确。补充撤销与整条业务链路测试和README。'''

LEGACY='''import json,subprocess,sys,tempfile
from pathlib import Path
from board.compat import add,summary
state={'schema':1,'tickets':{}}
row={'id':' A ','title':'工单','status':'open','priority':'high'}
new=add(state,row)
assert not state['tickets'] and new['tickets']['A']['id']=='A'
assert summary(new)=={'total':1,'open':1,'done':0}
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory);db=root/'db.json';data=root/'row.json'
    db.write_text(json.dumps(state));data.write_text(json.dumps(row))
    cmd=[sys.executable,'-m','board.cli',str(db)]
    r=subprocess.run(cmd+['add',str(data)],capture_output=True,text=True)
    assert r.returncode==0 and len(r.stdout.splitlines())==1,(r.returncode,r.stderr)
    r=subprocess.run(cmd+['report'],capture_output=True,text=True)
    assert r.returncode==0 and json.loads(r.stdout)=={'total':1,'open':1,'done':0}
print('LEGACY_PASSED')
'''
PUBLIC='''from copy import deepcopy
from board.migration import migrate
from board.planner import preview,apply_plan
from board.queries import select,stats
raw={'schema':0,'tickets':[{'key':' A ','text':'工单','closed':False,'level':2}]}
old=deepcopy(raw);state=migrate(raw)
assert raw==old and state['schema']==2 and state['tickets']['A']['tags']==[]
plan={'batch_id':'initial','expected_revision':0,'operations':[{'kind':'assign','id':'A','expected_revision':1,'owner':'产品'}]}
old=deepcopy(state);report=preview(state,plan)
assert state==old and report['next_revision']==1 and report['changed_ids']==['A']
if '--probe' not in __import__('sys').argv:
    new=apply_plan(state,plan)
    assert state==old and new['tickets']['A']['revision']==2
    assert apply_plan(new,{'batch_id':'initial','operations':None})==new
    assert select(new,{'owner':'产品'})[0]['id']=='A'
    assert stats(new,{})['total']==1
print('INITIAL_PASSED')
'''
PUBLIC_CHANGE='''from board.migration import migrate
from board.planner import apply_plan,preview
from board.rollback import rollback
from board.queries import stats
from board.audit import changes_since
state=migrate({'schema':1,'tickets':{'A':{'id':'A','title':'旧工单','status':'open','priority':'high'}}})
plan={'batch_id':'one','expected_revision':0,'operations':[{'kind':'transition','id':'A','expected_revision':1,'status':'done'}]}
new=apply_plan(state,plan);restored=rollback(new,'one','undo-one',1)
assert new['tickets']['A']['status']=='done' and restored['tickets']['A']['status']=='open'
assert restored['revision']==2 and restored['tickets']['A']['revision']==3 and restored['undo']==[]
assert stats(restored,{})['status']['open']==1 and len(changes_since(restored,0))==2
assert apply_plan(restored,plan)==restored
assert rollback(restored,'missing','undo-one',-7)==restored
try:preview(restored,{'batch_id':'stale','expected_revision':1,'operations':[]})
except ValueError:pass
else:raise AssertionError('must reject stale preview')
print('CHANGE_PASSED')
'''


REFERENCE_TESTS="import copy,unittest\nfrom board.migration import migrate\nfrom board.planner import preview,apply_plan\nfrom board.rollback import rollback\nfrom board.queries import select,stats\nclass Chain(unittest.TestCase):\n    def base(self):return migrate({'schema':0,'tickets':[{'key':'A','text':'Old','closed':False,'level':2}]})\n    def test_preview_apply_undo(self):\n        s=self.base();p={'batch_id':'one','expected_revision':0,'operations':[{'kind':'assign','id':'A','owner':'X','expected_revision':1}]}\n        self.assertEqual(preview(s,p)['next_revision'],1)\n        self.assertEqual(s['revision'],0)\n        n=apply_plan(s,p);r=rollback(n,'one','undo',1)\n        self.assertEqual(r['tickets']['A']['owner'],'')\n        self.assertEqual(r['tickets']['A']['revision'],3)\n    def test_atomic_failure(self):\n        s=self.base();before=copy.deepcopy(s)\n        with self.assertRaises(ValueError):apply_plan(s,{'batch_id':'bad','expected_revision':0,'operations':[{'kind':'assign','id':'A','owner':'X','expected_revision':1},{'kind':'delete','id':'missing','expected_revision':1}]})\n        self.assertEqual(s,before)\n    def test_query_stats(self):\n        s=self.base();self.assertEqual(select(s,{'priority':'high'})[0]['id'],'A')\n        self.assertEqual(stats(s,{'limit':0})['total'],0)\n"
GRADE="import copy,json,os,re,subprocess,sys,tempfile\nfrom pathlib import Path\nfrom unittest.mock import patch\nsys.path.insert(0,os.getcwd())\nfrom board import migration,schema,planner,queries,audit,rollback,repository,compat\n\ndef old():\n    return {'schema':0,'tickets':[{'key':' A ','text':'旧A','closed':False,'level':2},{'key':'B','text':'旧B','closed':True,'level':1}]}\ndef initial():return migration.migrate(old())\ndef plan(batch,revision,operations):return {'batch_id':batch,'expected_revision':revision,'operations':operations}\ndef rejects(fn,*args):\n    try:fn(*args)\n    except ValueError:return\n    raise AssertionError('必须拒绝非法或冲突输入')\ndef same(left,right):assert left==right\nchecks=[]\ndef check(ident,label,fn):\n    try:fn();checks.append({'id':ident,'label':label,'passed':True})\n    except Exception as error:checks.append({'id':ident,'label':label,'passed':False,'error':type(error).__name__+':'+str(error)})\ndef invoke(db,*args):\n    return subprocess.run([sys.executable,'-m','board.cli',str(db),*args],capture_output=True,text=True,timeout=12)\ndef good(result):\n    assert result.returncode==0 and not result.stderr and len(result.stdout.splitlines())==1,(result.returncode,result.stderr,result.stdout[:300])\n    return json.loads(result.stdout)\ndef fail(result,db,before):\n    assert result.returncode==2 and result.stderr and not result.stdout,(result.returncode,result.stdout,result.stderr)\n    assert db.read_bytes()==before\n\n\ndef legacy():\n    s={'schema':1,'tickets':{}};before=copy.deepcopy(s)\n    n=compat.add(s,{'id':' A ','title':'旧标题','status':'open','priority':'high'})\n    same(s,before);same(compat.summary(n),{'total':1,'open':1,'done':0})\n    rejects(compat.add,n,{'id':'A','title':'另一个','status':'open','priority':'high'})\n    with tempfile.TemporaryDirectory() as directory:\n        db=Path(directory)/'db.json';row=Path(directory)/'row.json';db.write_text(json.dumps(s));row.write_text(json.dumps({'id':'Z','title':'末尾','status':'done','priority':'low'}))\n        good(invoke(db,'add',str(row)));same(good(invoke(db,'report')),{'total':1,'open':0,'done':1})\ncheck('legacy','保留旧函数和旧CLI',legacy)\n\ndef migration_zero():\n    raw=old();before=copy.deepcopy(raw);s=migration.migrate(raw);same(raw,before)\n    assert s['schema']==2 and s['revision']==0 and s['batches']==s['audit']==s['undo']==[]\n    assert s['tickets']['A']=={'id':'A','title':'旧A','status':'open','priority':'high','owner':'','tags':[],'revision':1}\n    assert s['tickets']['B']['status']=='done' and s['tickets']['B']['priority']=='low'\n    for field,value in [('closed',1),('level',True),('level',3),('key',' '),('text',None)]:\n        bad=old();bad['tickets'][1][field]=value;before=copy.deepcopy(bad);rejects(migration.migrate,bad);same(bad,before)\ncheck('migration_zero','旧数组格式迁移与非法值',migration_zero)\n\ndef migration_one():\n    raw={'schema':1,'tickets':{' Q ':{'id':' Q ','title':' 新标题 ','status':'done','priority':'low'}}}\n    s=migration.migrate(raw);assert s['tickets']['Q']['title']=='新标题';same(migration.migrate(s),s)\n    bad=old();bad['tickets'].append({**bad['tickets'][0],'key':'A'});rejects(migration.migrate,bad)\n    rejects(migration.migrate,{'schema':True,'tickets':{}})\n    rejects(migration.migrate,{'schema':1,'tickets':{'mismatch':{'id':'A','title':'x','status':'open','priority':'high'}}})\n    s['tickets']['Q']['title']='changed';assert raw['tickets'][' Q ']['title']==' 新标题 '\ncheck('migration_one','字典格式、碰撞和迁移幂等',migration_one)\n\ndef schema_values():\n    row=schema.ticket({'id':' Z ','title':' 标题 ','tags':[' b ','a','b'],'owner':' 人 '})\n    same(row,{'id':'Z','title':'标题','status':'open','priority':'normal','owner':'人','tags':['a','b'],'revision':1})\n    for bad in [None,{'id':'A','title':'x','revision':True},{'id':'A','title':'x','tags':[' ']},{'id':'A','title':'x','other':1}]:rejects(schema.ticket,bad)\n    s=initial();n=schema.validate(s);n['tickets']['A']['tags'].append('x');assert not s['tickets']['A']['tags']\n    for field,value in [('revision',True),('schema',True),('batches',['one','one'])]:\n        bad=initial();bad[field]=value;rejects(schema.validate,bad)\n    bad=initial();bad['tickets']['A']['owner']=' spaced ';rejects(migration.migrate,bad)\ncheck('schema','规范数据与深复制',schema_values)\n\ndef preview_pure():\n    s=initial();before=copy.deepcopy(s);p=plan('preview',0,[{'kind':'assign','id':'A','owner':'甲','expected_revision':1},{'kind':'create','id':'C','title':'新增'}])\n    r=planner.preview(s,p);same(s,before)\n    same(r,{'batch_id':'preview','base_revision':0,'next_revision':1,'changed_ids':['A','C'],'operations_count':2,'tickets':3,'replayed':False})\n    n=planner.apply_plan(s,p);same(s,before);assert len(n['tickets'])==r['tickets']\ncheck('preview','预览无副作用且与执行一致',preview_pure)\n\ndef sequential():\n    s=initial();before=copy.deepcopy(s)\n    operations=[{'kind':'create','id':'C','title':'新增','owner':'甲','tags':['team',' urgent ']},{'kind':'update','id':'C','expected_revision':1,'changes':{'title':'改后','priority':'high'}},{'kind':'transition','id':'C','expected_revision':2,'status':'active'},{'kind':'assign','id':'A','expected_revision':1,'owner':''},{'kind':'delete','id':'B','expected_revision':1}]\n    n=planner.apply_plan(s,plan('sequence',0,operations));same(s,before)\n    assert n['tickets']['C']['revision']==3 and n['tickets']['C']['status']=='active' and n['tickets']['C']['title']=='改后' and 'B' not in n['tickets']\n    assert n['audit']==[{'revision':1,'batch_id':'sequence','kind':'apply','changed_ids':['A','B','C']}]\n    assert n['undo'][0]['before']==s['tickets'] and n['undo'][0]['changed_ids']==['A','B','C']\ncheck('sequential','多步依赖、创建更新分配删除',sequential)\n\ndef atomic_conflicts():\n    s=initial();before=copy.deepcopy(s)\n    first={'kind':'update','id':'A','expected_revision':1,'changes':{'title':'中间修改'}}\n    for final in [{'kind':'transition','id':'B','expected_revision':9,'status':'active'},{'kind':'delete','id':'missing','expected_revision':1},{'kind':'assign','id':'B','expected_revision':True,'owner':'甲'},{'kind':'update','id':'B','expected_revision':1,'changes':{'status':'active'}}]:\n        rejects(planner.apply_plan,s,plan('invalid',0,[first,final]));same(s,before)\n    rejects(planner.apply_plan,s,plan('stale',1,[]));rejects(planner.apply_plan,s,plan('boolean',True,[]))\n    n=planner.apply_plan(s,plan('empty',0,[]));assert n['revision']==1 and n['audit'][0]['changed_ids']==[]\ncheck('atomic','整批原子性、空批次和版本冲突',atomic_conflicts)\n\ndef replay():\n    s=planner.apply_plan(initial(),plan('one',0,[{'kind':'assign','id':'A','expected_revision':1,'owner':'甲'}]))\n    n=planner.apply_plan(s,{'batch_id':'one','operations':None,'expected_revision':-4,'extra':'ignored'});same(n,s)\n    r=planner.preview(s,{'batch_id':'one'});same(r,{'batch_id':'one','base_revision':1,'next_revision':1,'changed_ids':[],'operations_count':0,'tickets':2,'replayed':True})\n    n['tickets']['A']['owner']='not shared';assert s['tickets']['A']['owner']=='甲'\n    rejects(planner.apply_plan,s,{'batch_id':' one '})\ncheck('replay','重复批次深复制且不重复执行',replay)\n\ndef queried():\n    return planner.apply_plan(initial(),plan('query',0,[{'kind':'assign','id':'A','expected_revision':1,'owner':'甲'},{'kind':'update','id':'A','expected_revision':2,'changes':{'tags':['team','urgent']}},{'kind':'create','id':'C','title':'第三','priority':'normal','owner':'甲','tags':['team']},{'kind':'create','id':'D','title':'第四','status':'active','priority':'high','tags':['urgent']}]))\ndef query_rules():\n    s=queried();before=copy.deepcopy(s)\n    assert [r['id'] for r in queries.select(s,{'owner':'甲','tags_all':['team']})]==['A','C']\n    assert [r['id'] for r in queries.select(s,{'sort':'priority'})]==['A','D','C','B']\n    assert [r['id'] for r in queries.select(s,{'sort':'priority','offset':1,'limit':2})]==['D','C']\n    assert queries.select(s,{'limit':0})==[]\n    for q in [{'offset':True},{'limit':-1},{'status':'bad'},{'tags_all':[' ']},{'owner':' 甲 '},{'unknown':1}]:rejects(queries.select,s,q)\n    r=queries.select(s,{});r[0]['tags'].append('not shared');same(s,before)\ncheck('query','组合筛选、稳定排序和分页',query_rules)\n\ndef statistics():\n    s=queried();before=copy.deepcopy(s)\n    same(queries.stats(s,{'owner':'甲'}),{'total':2,'status':{'open':2,'active':0,'done':0},'priority':{'low':0,'normal':1,'high':1},'owners':{'甲':2},'tags':{'team':2,'urgent':1}})\n    same(queries.stats(s,{'limit':0}),{'total':0,'status':{'open':0,'active':0,'done':0},'priority':{'low':0,'normal':0,'high':0},'owners':{},'tags':{}})\n    assert queries.stats(s,{'limit':1})['total']==1;same(s,before)\ncheck('stats','统计与查询一致、零项和标签计数',statistics)\n\ndef audit_rules():\n    s=planner.apply_plan(queried(),plan('second',1,[]));before=copy.deepcopy(s)\n    rows=audit.changes_since(s,1);assert len(rows)==1 and rows[0]['batch_id']=='second'\n    assert audit.changes_since(s,2)==[];rejects(audit.changes_since,s,True)\n    rows[0]['changed_ids'].append('not shared');same(s,before)\ncheck('audit','审计版本过滤与数据隔离',audit_rules)\n\ndef persistence():\n    s=queried()\n    with tempfile.TemporaryDirectory() as directory:\n        db=Path(directory)/'nested/db.json';repository.save(s,db);same(repository.load(db),s);before=db.read_bytes()\n        with patch('os.replace',side_effect=OSError('offline failed replacement')):\n            try:repository.save(initial(),db)\n            except OSError:pass\n            else:raise AssertionError('save must fail')\n        assert db.read_bytes()==before and list(db.parent.iterdir())==[db]\n        db.write_text(json.dumps(old()));old_bytes=db.read_bytes();same(repository.load(db),initial());assert db.read_bytes()==old_bytes\n        db.write_text('{bad');rejects(repository.load,db)\ncheck('persistence','文件迁移、原子保存和失败保留',persistence)\n\ndef cli_initial():\n    with tempfile.TemporaryDirectory() as directory:\n        root=Path(directory);db=root/'db.json';pf=root/'plan.json';qf=root/'query.json'\n        db.write_text(json.dumps(old()));pf.write_text(json.dumps(plan('cli',0,[{'kind':'transition','id':'A','expected_revision':1,'status':'active'}])));qf.write_text('{}')\n        before=db.read_bytes();r=good(invoke(db,'preview',str(pf)));assert r['base_revision']==0 and db.read_bytes()==before\n        state=good(invoke(db,'apply',str(pf)));assert state['tickets']['A']['status']=='active'\n        before=db.read_bytes();identity=(db.stat().st_ino,db.stat().st_mtime_ns)\n        same(good(invoke(db,'apply',str(pf))),state);assert db.read_bytes()==before and (db.stat().st_ino,db.stat().st_mtime_ns)==identity\n        assert len(good(invoke(db,'query',str(qf))))==2 and good(invoke(db,'stats',str(qf)))['total']==2\n        assert len(good(invoke(db,'audit','0')))==1 and good(invoke(db,'report'))=={'total':2,'open':1,'done':1}\n        pf.write_text('{bad');fail(invoke(db,'apply',str(pf)),db,before);fail(invoke(db,'apply',str(root/'missing')),db,before)\n        pf.write_text(json.dumps(plan('stale',0,[])));fail(invoke(db,'apply',str(pf)),db,before)\n        assert db.read_bytes()==before\ncheck('cli_initial','文件输入与完整CLI业务链路',cli_initial)\n\ndef rollback_restore():\n    s=initial();before=copy.deepcopy(s)\n    n=planner.apply_plan(s,plan('change',0,[{'kind':'update','id':'A','expected_revision':1,'changes':{'title':'修改','owner':'甲','tags':['new']}},{'kind':'delete','id':'B','expected_revision':1},{'kind':'create','id':'C','title':'新增'}]))\n    r=rollback.rollback(n,'change','undo-change',1);same(s,before)\n    assert 'C' not in r['tickets'] and r['tickets']['A']['title']=='旧A' and r['tickets']['A']['revision']==3 and r['tickets']['B']['revision']==2\n    assert r['revision']==2 and r['batches']==['change','undo-change'] and r['undo']==[]\n    assert r['audit'][-1]=={'revision':2,'batch_id':'undo-change','kind':'rollback','source_batch_id':'change','changed_ids':['A','B','C']}\ncheck('rollback_restore','撤销创建删除修改并保持版本',rollback_restore)\n\ndef rollback_conflicts():\n    s=planner.apply_plan(initial(),plan('one',0,[]));s=planner.apply_plan(s,plan('two',1,[]));before=copy.deepcopy(s)\n    for source,batch,revision in [('one','bad',2),('missing','bad',2),('two','bad',1),('two','bad',True)]:rejects(rollback.rollback,s,source,batch,revision);same(s,before)\n    r=rollback.rollback(s,'two','undo',2);same(rollback.rollback(r,'missing','undo',-5),r)\n    same(planner.apply_plan(r,{'batch_id':'two','operations':None}),r)\n    rejects(planner.apply_plan,r,plan('outdated',2,[]))\ncheck('rollback_conflicts','撤销顺序、全局冲突与幂等',rollback_conflicts)\n\ndef repeated_undo():\n    s=initial();one=planner.apply_plan(s,plan('one',0,[{'kind':'assign','id':'A','expected_revision':1,'owner':'甲'}]))\n    two=planner.apply_plan(one,plan('two',1,[{'kind':'transition','id':'B','expected_revision':1,'status':'active'}]))\n    rb=rollback.rollback(two,'two','undo-two',2);version_b=rb['tickets']['B']['revision']\n    ra=rollback.rollback(rb,'one','undo-one',3)\n    assert ra['tickets']['B']['revision']==version_b and ra['tickets']['B']['status']=='done' and ra['tickets']['A']['owner']==''\n    assert ra['revision']==4 and ra['tickets']['A']['revision']==3 and ra['undo']==[]\n    assert [e['revision'] for e in audit.changes_since(ra,0)]==[1,2,3,4]\ncheck('repeated_undo','连续撤销不倒退无关工单版本',repeated_undo)\n\ndef cli_rollback():\n    with tempfile.TemporaryDirectory() as directory:\n        root=Path(directory);db=root/'db.json';pf=root/'plan.json';qf=root/'query.json'\n        repository.save(initial(),db);pf.write_text(json.dumps(plan('one',0,[{'kind':'transition','id':'A','expected_revision':1,'status':'done'}])));qf.write_text('{}')\n        good(invoke(db,'apply',str(pf)));r=good(invoke(db,'rollback','one','undo','1'))\n        assert r['revision']==2 and r['tickets']['A']['status']=='open'\n        before=db.read_bytes();identity=(db.stat().st_ino,db.stat().st_mtime_ns)\n        same(good(invoke(db,'rollback','missing','undo','-4')),r);assert (db.stat().st_ino,db.stat().st_mtime_ns)==identity\n        fail(invoke(db,'rollback','one','new-undo','2'),db,before)\n        assert good(invoke(db,'stats',str(qf)))['status']=={'open':1,'active':0,'done':1}\n        assert good(invoke(db,'report'))=={'total':2,'open':1,'done':1}\n        assert good(invoke(db,'audit','1'))[0]['kind']=='rollback'\n        assert db.read_bytes()==before\ncheck('cli_rollback','撤销后的CLI、查询统计和审计',cli_rollback)\n\ndef delivered_tests_docs():\n    readme=Path('README.md').read_text(encoding='utf-8')\n    assert all(word in readme for word in ('python -m board.cli','preview','apply','rollback','unittest')),'README需有新增功能运行与测试说明'\n    assert Path('tests').is_dir(),'需要交付项目测试'\n    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],capture_output=True,text=True,timeout=20)\n    match=re.search(r'Ran (\\d+) tests?',result.stderr)\n    assert result.returncode==0 and match and int(match.group(1))>=3,(result.returncode,result.stderr[:1000])\ncheck('tests_docs','交付可运行测试与README',delivered_tests_docs)\nprint(json.dumps({'passed':all(c['passed'] for c in checks),'checks':checks},ensure_ascii=False))\nraise SystemExit(0 if all(c['passed'] for c in checks) else 1)\n"

def write(path,source):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(textwrap.dedent(source),encoding='utf-8')


def prepare(package=PACKAGE):
    package=inside_project(package)
    if package.exists():
        raise ValueError('题包已存在，不覆盖历史版本')
    functions={'schema.py':['ticket','validate'],'migration.py':['migrate'],'planner.py':['preview','apply_plan'],'queries.py':['select','stats'],'audit.py':['changes_since'],'rollback.py':['rollback']}
    for name,names in functions.items():
        write(package/'reference/board'/name,'from .domain import '+','.join(names)+'\n')
        stubs='\n\n'.join('def '+n+'('+({'ticket':'row','validate':'state','migrate':'raw','preview':'state,plan','apply_plan':'state,plan','select':'state,query','stats':'state,query','changes_since':'state,since','rollback':'state,source_batch_id,rollback_batch_id,expected_revision'}[n])+'):\n    """Implement the public README contract without mutating inputs."""\n    raise NotImplementedError' for n in names)+'\n'
        write(package/'initial/board'/name,stubs)
    for directory in ('initial','reference'):
        write(package/directory/'board/__init__.py','')
        write(package/directory/'board/compat.py',COMPAT)
    write(package/'initial/board/repository.py',INITIAL_REPOSITORY)
    write(package/'initial/board/cli.py',INITIAL_CLI)
    write(package/'reference/board/domain.py',DOMAIN)
    write(package/'reference/board/repository.py',REPOSITORY)
    write(package/'reference/board/cli.py',CLI)
    write(package/'initial/README.md',DESCRIPTION)
    write(package/'initial/tests/test_legacy.py',"import unittest\nfrom board.compat import add,summary\nclass Legacy(unittest.TestCase):\n    def test_add_and_report(self):\n        source={'schema':1,'tickets':{}}\n        new=add(source,{'id':' A ','title':'Old','status':'open','priority':'high'})\n        self.assertEqual(summary(new),{'total':1,'open':1,'done':0})\n        self.assertEqual(source['tickets'],{})\n")
    for name,value in [('public_legacy.py',LEGACY),('public_initial.py',PUBLIC),('public_change.py',PUBLIC_CHANGE)]:
        write(package/'initial'/name,value)
    python=str(PROJECT/'.venv/bin/python')
    task=Task(id='L1-v1',objective='升级已有工单项目：迁移两种旧数据、实现批量变更预览和原子执行、版本冲突与幂等、查询报表和可追溯审计，保留旧API与CLI，补充测试和README。',constraints=['只用Python标准库，不访问公网，不修改规定检查。','所有文件路径、数据结构与失败约定按README执行，保留旧接口。'],acceptance=['整条业务链路正确；输入不变、失败不破坏数据，公开检查通过。'],write_paths=['board/','tests/','README.md'],protected_paths=['public_legacy.py','public_initial.py','public_change.py'],checks=[{'id':'legacy','argv':[python,'public_legacy.py'],'timeout':20},{'id':'initial','argv':[python,'public_initial.py'],'timeout':20}],feedback={'probe':{'id':'migration_preview','argv':[python,'public_initial.py','--probe'],'timeout':15},'text':CHANGE,'acceptance':['撤销后版本单调、原批次幂等、查询审计一致，旧功能保留。'],'checks':[{'id':'change','argv':[python,'public_change.py'],'timeout':20}]})
    write(package/'task.json',task.model_dump_json(indent=2))
    write(package/'description.md',DESCRIPTION+'\n\n## 中途追加要求\n'+CHANGE)
    write(package/'grade.py',GRADE)
    write(package/'reference/README.md',DESCRIPTION+'\n'+CHANGE+'\n测试：python -m unittest discover -s tests -v\n')
    write(package/'reference/tests/test_chain.py',REFERENCE_TESTS)
    write(package/'reference/tests/test_legacy.py',(package/'initial/tests/test_legacy.py').read_text())
    write(package/'contract.json',json.dumps({'version':'L1-v1','origin':'fresh authored task and reference; no historical model answers','classification':'moderate technical difficulty, long linked workflow candidate','human_duration_calibrated':False,'feedback_trigger':'first successful migration_preview probe','time_seconds_per_group':1800,'tokens':'record only, no cumulative threshold','feedback_text':CHANGE},ensure_ascii=False,indent=2))
    return package


if __name__=='__main__':
    print(prepare())
