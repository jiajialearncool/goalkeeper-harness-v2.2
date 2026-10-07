"""Author a fresh hard fulfillment task, independent of historical model deliveries."""
import importlib.util
import json
from pathlib import Path
import textwrap

from goalkeeper.models import Task
from goalkeeper.paths import PROJECT, inside_project

PACKAGE = PROJECT/'evaluation/private/H1-v1'

LEGACY = r'''from copy import deepcopy

def receive(state,row):
    if not isinstance(state,dict) or set(state)!={'schema','stocks'} or type(state['schema']) is not int or state['schema']!=1 or not isinstance(state['stocks'],dict): raise ValueError('legacy state')
    if any(not isinstance(k,str) or not k or k.strip()!=k or type(v) is not int or v<0 for k,v in state['stocks'].items()): raise ValueError('stocks')
    if not isinstance(row,dict) or set(row)!={'sku','quantity'}: raise ValueError('receipt')
    sku=row['sku'];qty=row['quantity']
    if not isinstance(sku,str) or not sku.strip() or type(qty) is not int or qty<=0: raise ValueError('receipt values')
    out=deepcopy(state);sku=sku.strip();out['stocks'][sku]=out['stocks'].get(sku,0)+qty;return out

def report(state):
    # The historical public result is retained for both legacy and upgraded CLI.
    if not isinstance(state,dict) or set(state)!={'schema','stocks'} or type(state['schema']) is not int or state['schema']!=1: raise ValueError('legacy state')
    stocks=state['stocks']
    if not isinstance(stocks,dict) or any(not isinstance(k,str) or not k or k.strip()!=k or type(v) is not int or v<0 for k,v in stocks.items()): raise ValueError('stocks')
    return {'units':sum(stocks.values()),'skus':dict(sorted(stocks.items()))}
'''

CODEC = r'''import json
from pathlib import Path

def read_json(path):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError,UnicodeError) as error:raise ValueError('invalid JSON') from error

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
'''

INITIAL_REPOSITORY = r'''from pathlib import Path
from .codec import read_json,canonical
from .legacy import report

def load(path):
    state=read_json(path);report(state);return state

def save(state,path):
    report(state);Path(path).write_text(canonical(state),encoding='utf-8')
'''

INITIAL_CLI = r'''import argparse,json,sys
from . import legacy,repository
from .codec import read_json

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('database');sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('receive').add_argument('input');sub.add_parser('report');args=parser.parse_args(argv)
    try:
        state=repository.load(args.database)
        if args.command=='receive':state=legacy.receive(state,read_json(args.input));repository.save(state,args.database);result=state
        else:result=legacy.report(state)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (ValueError,OSError) as error:print(str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
'''

CORE = r'''from copy import deepcopy
from hashlib import sha256
from .codec import canonical

ZERO='0'*64
STATUSES=('open','reserved','partial','fulfilled','cancelled','closed')

def ident(value):
    if not isinstance(value,str) or not value or value.strip()!=value:raise ValueError('canonical identifier')
    return value

def integer(value,minimum=0):
    if type(value) is not int or value<minimum:raise ValueError('integer')
    return value

def fields(value,required,optional=()):
    if not isinstance(value,dict) or not set(required)<=set(value) or set(value)-set(required)-set(optional):raise ValueError('fields')

def lot(row):
    fields(row,('id','sku','warehouse','expires_at','on_hand','reserved'))
    for k in ('id','sku','warehouse'):ident(row[k])
    if row['expires_at'] is not None:integer(row['expires_at'])
    integer(row['on_hand']);integer(row['reserved'])
    if row['reserved']>row['on_hand']:raise ValueError('reserved stock exceeds on hand')
    return deepcopy(row)

def status(order):
    lines=order['lines'].values();remaining=sum(x['quantity']-x['shipped']-x['cancelled'] for x in lines)
    shipped=sum(x['shipped'] for x in order['lines'].values());cancelled=sum(x['cancelled'] for x in order['lines'].values())
    if not remaining:return 'cancelled' if not shipped else 'fulfilled' if not cancelled else 'closed'
    if shipped:return 'partial'
    return 'reserved' if any(x['allocations'] for x in order['lines'].values()) else 'open'

def validate(state):
    fields(state,('schema','revision','clock','genesis','lots','orders','batches','events','head','ledger'))
    if type(state['schema']) is not int or state['schema']!=2:raise ValueError('schema')
    integer(state['revision']);integer(state['clock'])
    for name in ('genesis','lots'):
        if not isinstance(state[name],dict):raise ValueError(name)
        for key,row in state[name].items():
            if lot(row)!=row or key!=row['id'] or name=='genesis' and row['reserved']!=0:raise ValueError('lot')
    if not isinstance(state['orders'],dict):raise ValueError('orders')
    allocated={key:0 for key in state['lots']}
    for key,order in state['orders'].items():
        fields(order,('id','warehouse','expires_at','status','lines'))
        if ident(order['id'])!=key or order['status'] not in STATUSES:raise ValueError('order id/status')
        ident(order['warehouse']);integer(order['expires_at'])
        if not isinstance(order['lines'],dict) or not order['lines']:raise ValueError('lines')
        for sku,line in order['lines'].items():
            ident(sku);fields(line,('quantity','unit_price','shipped','cancelled','returned','allocations'))
            integer(line['quantity'],1)
            for k in ('unit_price','shipped','cancelled','returned'):integer(line[k])
            if line['shipped']+line['cancelled']>line['quantity'] or line['returned']>line['shipped']:raise ValueError('line totals')
            if not isinstance(line['allocations'],list):raise ValueError('allocations')
            held=0;seen=set()
            for a in line['allocations']:
                fields(a,('lot_id','quantity'));ident(a['lot_id']);integer(a['quantity'],1)
                if a['lot_id'] in seen or a['lot_id'] not in state['lots']:raise ValueError('allocation lot')
                seen.add(a['lot_id']);l=state['lots'][a['lot_id']]
                if l['sku']!=sku or l['warehouse']!=order['warehouse']:raise ValueError('allocation sku/warehouse')
                allocated[a['lot_id']]+=a['quantity'];held+=a['quantity']
            if held>line['quantity']-line['shipped']-line['cancelled']:raise ValueError('allocation exceeds remaining')
        if status(order)!=order['status']:raise ValueError('derived status')
    if any(state['lots'][key]['reserved']!=qty for key,qty in allocated.items()):raise ValueError('reservation conservation')
    if not isinstance(state['batches'],list) or any(ident(x)!=x for x in state['batches']) or len(set(state['batches']))!=len(state['batches']):raise ValueError('batches')
    if not isinstance(state['events'],list) or len(state['events'])!=state['revision'] or len(state['batches'])!=state['revision']:raise ValueError('event count')
    previous=ZERO;clock=0
    for number,event in enumerate(state['events'],1):
        fields(event,('revision','request_id','now','actions','previous','hash'))
        if type(event['revision']) is not int or event['revision']!=number or event['request_id']!=state['batches'][number-1] or event['previous']!=previous:raise ValueError('event sequence')
        integer(event['now']);ident(event['request_id'])
        if event['now']<clock or not isinstance(event['actions'],list):raise ValueError('event time/actions')
        if not isinstance(event['hash'],str) or len(event['hash'])!=64 or any(c not in '0123456789abcdef' for c in event['hash']):raise ValueError('hash format')
        previous=event['hash'];clock=event['now']
    if state['head']!=previous or state['clock']!=clock:raise ValueError('head/time')
    if not isinstance(state['ledger'],list):raise ValueError('ledger')
    for entry in state['ledger']:
        fields(entry,('kind','order_id','sku','quantity','unit_price','amount','revision','request_id'))
        if entry['kind'] not in ('sale','refund'):raise ValueError('ledger kind')
        for k in ('order_id','sku','request_id'):ident(entry[k])
        integer(entry['quantity'],1);integer(entry['unit_price']);integer(entry['amount']);integer(entry['revision'],1)
        if entry['revision']>state['revision'] or entry['amount']!=entry['quantity']*entry['unit_price']:raise ValueError('ledger amount/revision')
    return deepcopy(state)

def seed(lots):
    return dict(schema=2,revision=0,clock=0,genesis=deepcopy(lots),lots=deepcopy(lots),orders={},batches=[],events=[],head=ZERO,ledger=[])

def migrate(raw):
    if not isinstance(raw,dict) or type(raw.get('schema')) is not int:raise ValueError('schema')
    if raw['schema']==2:return validate(raw)
    if raw['schema']==0:
        fields(raw,('schema','rows'))
        if not isinstance(raw['rows'],list):raise ValueError('legacy rows')
        pairs=[]
        for row in raw['rows']:
            fields(row,('sku','quantity'));pairs.append((row['sku'],row['quantity']))
    elif raw['schema']==1:
        fields(raw,('schema','stocks'))
        if not isinstance(raw['stocks'],dict):raise ValueError('legacy stocks')
        pairs=list(raw['stocks'].items())
    else:raise ValueError('schema')
    lots={};seen=set()
    for sku,qty in pairs:
        if not isinstance(sku,str) or not sku.strip():raise ValueError('sku')
        sku=sku.strip();integer(qty)
        if sku in seen:raise ValueError('normalized duplicate sku')
        seen.add(sku);key='legacy:'+sku
        lots[key]=dict(id=key,sku=sku,warehouse='main',expires_at=None,on_hand=qty,reserved=0)
    return seed(lots)

def sorted_lots(state,sku,warehouse,now):
    return sorted((l for l in state['lots'].values() if l['sku']==sku and l['warehouse']==warehouse and (l['expires_at'] is None or l['expires_at']>now)),key=lambda l:(l['expires_at'] is None,l['expires_at'] if l['expires_at'] is not None else 0,l['id']))

def order_for(state,key):
    ident(key)
    if key not in state['orders']:raise ValueError('missing order')
    return state['orders'][key]

def release(state,order):
    for line in order['lines'].values():
        for a in line['allocations']:state['lots'][a['lot_id']]['reserved']-=a['quantity']
        line['allocations']=[]
    order['status']=status(order)

def cancel(state,order):
    release(state,order)
    for line in order['lines'].values():line['cancelled']=line['quantity']-line['shipped']
    order['status']=status(order)

def line_items(items,fields_required):
    if not isinstance(items,list) or not items:raise ValueError('items')
    seen=set()
    for item in items:
        fields(item,fields_required);ident(item['sku']);integer(item['quantity'],1)
        if item['sku'] in seen:raise ValueError('duplicate sku')
        seen.add(item['sku'])
    return items

def act(state,action,now,request,revision):
    if not isinstance(action,dict):raise ValueError('action')
    kind=action.get('kind')
    if kind=='receive':
        fields(action,('kind','lot_id','sku','warehouse','quantity','expires_at'))
        for k in ('lot_id','sku','warehouse'):ident(action[k])
        integer(action['quantity'],1)
        if action['expires_at'] is not None and integer(action['expires_at'])<=now:raise ValueError('already expired lot')
        if action['lot_id'] in state['lots']:raise ValueError('duplicate lot')
        state['lots'][action['lot_id']]=dict(id=action['lot_id'],sku=action['sku'],warehouse=action['warehouse'],expires_at=action['expires_at'],on_hand=action['quantity'],reserved=0)
    elif kind=='transfer':
        fields(action,('kind','lot_id','quantity','warehouse','new_lot_id'))
        for k in ('lot_id','warehouse','new_lot_id'):ident(action[k])
        integer(action['quantity'],1)
        if action['lot_id'] not in state['lots'] or action['new_lot_id'] in state['lots']:raise ValueError('lot')
        source=state['lots'][action['lot_id']]
        if action['warehouse']==source['warehouse'] or action['quantity']>source['on_hand']-source['reserved']:raise ValueError('transfer stock')
        if source['expires_at'] is not None and source['expires_at']<=now:raise ValueError('expired transfer')
        source['on_hand']-=action['quantity'];new=deepcopy(source)
        new.update(id=action['new_lot_id'],warehouse=action['warehouse'],on_hand=action['quantity'],reserved=0)
        state['lots'][new['id']]=new
    elif kind=='order':
        fields(action,('kind','id','warehouse','expires_at','lines'))
        ident(action['id']);ident(action['warehouse'])
        if action['id'] in state['orders'] or integer(action['expires_at'])<=now:raise ValueError('order id/expiry')
        rows=line_items(action['lines'],('sku','quantity','unit_price'));lines={}
        for row in rows:
            integer(row['unit_price']);lines[row['sku']]=dict(quantity=row['quantity'],unit_price=row['unit_price'],shipped=0,cancelled=0,returned=0,allocations=[])
        state['orders'][action['id']]=dict(id=action['id'],warehouse=action['warehouse'],expires_at=action['expires_at'],status='open',lines=lines)
    elif kind=='reserve':
        fields(action,('kind','order_id'));order=order_for(state,action['order_id'])
        if order['expires_at']<=now:raise ValueError('expired order')
        for sku,line in order['lines'].items():
            if any(state['lots'][a['lot_id']]['expires_at'] is not None and state['lots'][a['lot_id']]['expires_at']<=now for a in line['allocations']):raise ValueError('release expired allocation first')
            remaining=line['quantity']-line['shipped']-line['cancelled']-sum(a['quantity'] for a in line['allocations'])
            for l in sorted_lots(state,sku,order['warehouse'],now):
                take=min(remaining,l['on_hand']-l['reserved'])
                if take:
                    existing=next((a for a in line['allocations'] if a['lot_id']==l['id']),None)
                    if existing:existing['quantity']+=take
                    else:line['allocations'].append({'lot_id':l['id'],'quantity':take})
                    l['reserved']+=take;remaining-=take
            if remaining:raise ValueError('insufficient stock')
        order['status']=status(order)
    elif kind=='ship':
        fields(action,('kind','order_id','items'));order=order_for(state,action['order_id'])
        if order['expires_at']<=now:raise ValueError('expired order')
        for item in line_items(action['items'],('sku','quantity')):
            if item['sku'] not in order['lines']:raise ValueError('sku not in order')
            line=order['lines'][item['sku']];remaining=item['quantity']
            if remaining>sum(a['quantity'] for a in line['allocations']):raise ValueError('shipment exceeds reservation')
            for a in line['allocations']:
                l=state['lots'][a['lot_id']]
                if l['expires_at'] is not None and l['expires_at']<=now:raise ValueError('expired shipment lot')
                take=min(remaining,a['quantity']);a['quantity']-=take;l['reserved']-=take;l['on_hand']-=take;remaining-=take
                if not remaining:break
            line['allocations']=[a for a in line['allocations'] if a['quantity']]
            line['shipped']+=item['quantity']
            state['ledger'].append(dict(kind='sale',order_id=order['id'],sku=item['sku'],quantity=item['quantity'],unit_price=line['unit_price'],amount=item['quantity']*line['unit_price'],revision=revision,request_id=request))
        order['status']=status(order)
    elif kind in ('release','cancel'):
        fields(action,('kind','order_id'));order=order_for(state,action['order_id'])
        (release if kind=='release' else cancel)(state,order)
    elif kind=='expire':
        fields(action,('kind',))
        for order in state['orders'].values():
            if order['expires_at']<=now:cancel(state,order)
    elif kind=='return':
        fields(action,('kind','order_id','warehouse','items'));order=order_for(state,action['order_id']);ident(action['warehouse'])
        for item in line_items(action['items'],('sku','quantity','lot_id')):
            ident(item['lot_id'])
            if item['sku'] not in order['lines'] or item['lot_id'] in state['lots']:raise ValueError('return sku/lot')
            line=order['lines'][item['sku']]
            if item['quantity']>line['shipped']-line['returned']:raise ValueError('excess return')
            line['returned']+=item['quantity']
            state['lots'][item['lot_id']]=dict(id=item['lot_id'],sku=item['sku'],warehouse=action['warehouse'],expires_at=None,on_hand=item['quantity'],reserved=0)
            state['ledger'].append(dict(kind='refund',order_id=order['id'],sku=item['sku'],quantity=item['quantity'],unit_price=line['unit_price'],amount=item['quantity']*line['unit_price'],revision=revision,request_id=request))
        # A return never reopens fulfillment or re-reserves stock.
    else:raise ValueError('unknown action')

def hash_event(body):return sha256(canonical(body).encode('utf-8')).hexdigest()

def plan_result(state,envelope):
    original=validate(state)
    if not isinstance(envelope,dict):raise ValueError('envelope')
    request=ident(envelope.get('request_id'))
    replay=request in original['batches']
    if replay:return original,dict(request_id=request,base_revision=original['revision'],next_revision=original['revision'],changed_lots=[],changed_orders=[],gross_delta=0,refund_delta=0,replayed=True)
    fields(envelope,('request_id','expected_revision','now','actions'))
    if integer(envelope['expected_revision'])!=original['revision'] or integer(envelope['now'])<original['clock']:raise ValueError('revision/time conflict')
    if not isinstance(envelope['actions'],list):raise ValueError('actions')
    out=deepcopy(original);revision=out['revision']+1;now=envelope['now']
    for action in envelope['actions']:act(out,action,now,request,revision)
    changed_lots=sorted(k for k in out['lots'] if out['lots'][k]!=original['lots'].get(k))
    changed_orders=sorted(k for k in out['orders'] if out['orders'][k]!=original['orders'].get(k))
    ledger=out['ledger'][len(original['ledger']):]
    report=dict(request_id=request,base_revision=original['revision'],next_revision=revision,changed_lots=changed_lots,changed_orders=changed_orders,gross_delta=sum(e['amount'] for e in ledger if e['kind']=='sale'),refund_delta=sum(e['amount'] for e in ledger if e['kind']=='refund'),replayed=False)
    body=dict(revision=revision,request_id=request,now=now,actions=deepcopy(envelope['actions']),previous=out['head'])
    event={**body,'hash':hash_event(body)};out['events'].append(event);out['head']=event['hash'];out['batches'].append(request);out['revision']=revision;out['clock']=now
    return validate(out),report

def preview(state,envelope):return plan_result(state,envelope)[1]
def apply_batch(state,envelope):return plan_result(state,envelope)[0]

def stock(state,query):
    data=validate(state);fields(query,(),('sku','warehouse','available_only'))
    for k in ('sku','warehouse'):
        if k in query:ident(query[k])
    if 'available_only' in query and type(query['available_only']) is not bool:raise ValueError('available_only')
    result=[]
    for row in data['lots'].values():
        if any(row[k]!=query[k] for k in ('sku','warehouse') if k in query):continue
        available=row['on_hand']-row['reserved'] if row['expires_at'] is None or row['expires_at']>data['clock'] else 0
        if query.get('available_only',False) and not available:continue
        result.append({**row,'available':available})
    return sorted(result,key=lambda r:(r['warehouse'],r['sku'],r['expires_at'] is None,r['expires_at'] if r['expires_at'] is not None else 0,r['id']))

def orders(state,query):
    data=validate(state);fields(query,(),('status','warehouse','sku','offset','limit'))
    for k in ('warehouse','sku'):
        if k in query:ident(query[k])
    if 'status' in query and query['status'] not in STATUSES:raise ValueError('status')
    offset=integer(query.get('offset',0));limit=query.get('limit')
    if limit is not None:integer(limit)
    rows=sorted((o for o in data['orders'].values() if all(o[k]==query[k] for k in ('status','warehouse') if k in query) and ('sku' not in query or query['sku'] in o['lines'])),key=lambda o:o['id'])
    return rows[offset:] if limit is None else rows[offset:offset+limit]

def revenue(state,query):
    rows=orders(state,query);gross=refunded=units=returned=0
    for o in rows:
        for line in o['lines'].values():
            gross+=line['shipped']*line['unit_price'];refunded+=line['returned']*line['unit_price'];units+=line['shipped'];returned+=line['returned']
    return dict(gross=gross,refunded=refunded,net=gross-refunded,shipped_units=units,returned_units=returned)

def report(state):
    data=validate(state);stocks={}
    for row in data['lots'].values():stocks[row['sku']]=stocks.get(row['sku'],0)+row['on_hand']
    return {'units':sum(stocks.values()),'skus':dict(sorted(stocks.items()))}

def changes_since(state,since):
    data=validate(state);integer(since);return deepcopy([e for e in data['events'] if e['revision']>since])

def rebuild(document):
    if not isinstance(document,dict) or not isinstance(document.get('genesis'),dict) or not isinstance(document.get('events'),list):raise ValueError('journal document')
    start=seed(document['genesis']);validate(start)
    for event in document['events']:
        fields(event,('revision','request_id','now','actions','previous','hash'))
        body={k:v for k,v in event.items() if k!='hash'}
        if event['previous']!=start['head'] or event['hash']!=hash_event(body) or type(event['revision']) is not int or event['revision']!=start['revision']+1:raise ValueError('tampered journal')
        envelope={'request_id':event['request_id'],'expected_revision':start['revision'],'now':event['now'],'actions':event['actions']}
        start=apply_batch(start,envelope)
        if start['events'][-1]!=event:raise ValueError('journal replay mismatch')
    return start

def verify(state):
    data=validate(state)
    if data!=rebuild(data):raise ValueError('projection mismatch')
    return True
'''

REPOSITORY = r'''import os,tempfile
from pathlib import Path
from .codec import read_json,canonical
from .migration import migrate
from .schema import validate

def load(path):return migrate(read_json(path))

def save(state,path):
    value=validate(state);path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
'''

CLI = r'''import argparse,json,sys
from . import repository,planner,queries,finance,journal,migration
from .codec import read_json

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('database');sub=parser.add_subparsers(dest='command',required=True)
    for name in ('receive','preview','apply','stock','orders','revenue'):sub.add_parser(name).add_argument('input')
    for name in ('migrate','report','verify','rebuild'):sub.add_parser(name)
    sub.add_parser('audit').add_argument('since',type=int);args=parser.parse_args(argv)
    try:
        raw=read_json(args.database)
        if args.command=='rebuild':result=journal.rebuild(raw);repository.save(result,args.database)
        else:
            state=migration.migrate(raw)
            if args.command=='migrate':
                result=state
                if raw['schema']!=2:repository.save(state,args.database)
            elif args.command=='report':result=queries.report(state)
            elif args.command=='receive':
                from .legacy import receive
                receipt=read_json(args.input);validated=receive({'schema':1,'stocks':{}},receipt)['stocks'];sku,qty=next(iter(validated.items()))
                envelope=dict(request_id=f"legacy:{state['revision']}:{sku}",expected_revision=state['revision'],now=state['clock'],actions=[dict(kind='receive',lot_id=f"receipt:{state['revision']}:{sku}",sku=sku,quantity=qty,warehouse='main',expires_at=None)])
                result=planner.apply_batch(state,envelope);repository.save(result,args.database)
            elif args.command in ('preview','apply'):
                envelope=read_json(args.input)
                if args.command=='preview':result=planner.preview(state,envelope)
                else:
                    result=planner.apply_batch(state,envelope)
                    if envelope['request_id'] not in state['batches']:repository.save(result,args.database)
            elif args.command in ('stock','orders'):result=getattr(queries,args.command)(state,read_json(args.input))
            elif args.command=='revenue':result=finance.revenue(state,read_json(args.input))
            elif args.command=='audit':result=journal.changes_since(state,args.since)
            else:journal.verify(state);result={'valid':True,'revision':state['revision'],'head':state['head']}
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (ValueError,OSError) as error:print(str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
'''

DESCRIPTION = '''# H1-v1：升级库存工具，完成多仓订单履约与可核对的业务日志

这是已有 supply 库存小项目的改造。legacy.receive/report、codec、repository和CLI receive/report已有实现，现有tests必须保留。只使用Python标准库，不访问公网。实现下列完整业务链、至少8项unittest项目测试及README运行说明。新接口桩不限制内部组织方式，可增加supply下的模块。

## 一致的数据结构
所有标识符是非空且无首尾空格的字符串；除明确说明的旧数据/旧receive外不自动去空格。所有数量、价格、版本、时间必须是int而非bool；数量正整数，其他计数及时间非负。金额单位是分，只做整数运算。API任一非法、冲突或不足输入均抛ValueError。函数不改变任何调用方输入；查询、预览、迁移和执行结果都隔离嵌套对象。

schema=2状态恰含schema/revision/clock/genesis/lots/orders/batches/events/head/ledger。schema必须整数2。初始revision=clock=0，orders={}，batches/events/ledger=[]，head为64个'0'。genesis是开账库存字典的永久深复制，不能随业务修改。
- lots字典以lot.id为键；每行恰含id/sku/warehouse/expires_at/on_hand/reserved。expires_at为非负整数或null；on_hand>=reserved>=0。保留数量为0的lot。
- orders以order.id为键；每行恰含id/warehouse/expires_at/status/lines。lines以sku为键、非空；行恰含quantity/unit_price/shipped/cancelled/returned/allocations。quantity>0，其余整数非负，unit_price允许0；shipped+cancelled<=quantity，returned<=shipped。allocations为[{lot_id,quantity}]，同一行lot_id不重复，quantity>0；lot必须同sku、同warehouse。各lot.reserved必须等于所有订单对该lot的预占之和；每行预占不超过quantity-shipped-cancelled。
- status为派生值：尚有未发货/未取消数量时，有shipped则partial，否则有预占则reserved，否则open；全部处理完时，shipped=0为cancelled，没有cancelled为fulfilled，否则closed。returned不影响履约status。
- batches为按提交顺序的唯一request_id数组；每个提交只有一个event。events长度、batches长度均等于revision。clock等于最后event.now，空日志clock=0。
- event恰含revision/request_id/now/actions/previous/hash；revision从1连续递增，request_id与batches相应项一致，now非负单调不降，actions数组深复制。previous指前一个event.hash（首个64个'0'），head指最后hash。
- hash：从event删去hash键，将其余字段用json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')，再取hashlib.sha256(...).hexdigest()。
- ledger是对象数组；行恰含kind/order_id/sku/quantity/unit_price/amount/revision/request_id。kind为sale或refund；quantity>0，unit_price及amount非负，amount=quantity*unit_price，revision在1..状态revision。ship产生sale，returned初始为0，refund后续需求到达时再定义。每个items行按给定顺序各产生一条entry，不合并不同action。全批共享新revision/request_id。

supply.schema.validate(state) 校验上述结构、标识、类型、数量、派生status、预占守恒、日志连续性、hash格式和previous/head/clock的一致性，返回深复制。本阶段不要求它重新执行日志或计算hash内容；后续verify会核对。genesis的lot全部reserved=0。

## 迁移与旧接口
supply.migration.migrate(raw)：schema=0恰含schema/rows，rows为[{sku,quantity}]；schema=1恰含schema/stocks，stocks是sku到数量的字典。旧sku去首尾空格、必须非空，规范化后碰撞报错，quantity非负且非bool。迁移为warehouse='main'、expires_at=null、reserved=0的lot，id='legacy:'+sku；genesis与lots隔离。schema=2只验证并深复制，不偷偷修复；migrate(migrate(raw))一致。非法记录导致整次失败。
legacy.receive(state,{sku,quantity})保留schema1接口：sku去空格，quantity>0，返回新增库存的深复制，不改原对象。legacy.report(schema1)返回{units:数量总和,skus:按sku排序的数量字典}。升级后的CLI report输出同一形状，按所有仓所有lot.on_hand汇总（包含过期及预占，保留零库存sku）。旧CLI receive仍读JSON文件{sku,quantity}，创建warehouse=main、expires_at=null的新lot，并返回schema2状态；不改变调用形式。

## 批量命令、预览和事务
supply.planner.preview(state,envelope)与apply_batch(state,envelope)。envelope恰含request_id/expected_revision/now/actions。request_id必须规范；若已在batches中，立即返回当前状态深复制或回放预览，忽略其他字段/旧版本/错误now/actions，不增加日志。若不是回放，expected_revision必须等于数据库revision，now>=clock；actions数组可为空。
各action按给定顺序在临时状态执行，相互依赖是允许的。任一步失败，整批无副作用。成功时revision+1，clock=now，追加一个event和batch，保持genesis。preview返回恰含request_id/base_revision/next_revision/changed_lots/changed_orders/gross_delta/refund_delta/replayed；变更ID升序，按最终lot/order对象与批前比较；delta为本批新增sale/refund金额合计。回放的next=base=current revision，变更数组空、delta为0、replayed=true。

action字段均恰如下，无额外字段：
- receive: {kind:'receive',lot_id,sku,warehouse,quantity,expires_at}。新lot_id唯一；quantity>0，expires_at为null或整数且>now。创建on_hand=quantity/reserved=0，不改genesis。
- transfer: {kind:'transfer',lot_id,quantity,warehouse,new_lot_id}。目标warehouse与原仓不同，新ID唯一；只能移动未预占数量，不能移动已过期lot。源on_hand减数量，目标新lot继承sku/expires_at，数量等于移动量、reserved=0。
- order: {kind:'order',id,warehouse,expires_at,lines:[{sku,quantity,unit_price}]}。新order.id唯一、expires_at>now，lines非空且sku不重复；shipped/cancelled/returned=0、allocations=[]、status=open。
- reserve: {kind:'reserve',order_id}。订单不能已到期（expires_at<=now）；在该仓为每个sku补齐全部未发货/未取消且未预占数量。库存分配按expires_at升序、null最后、相同日期按lot.id升序（FEFO）；不使用expires_at<=now的lot，不跨仓。保留已有预占、不重复占库存；任何sku不足则整批失败。已有预占含过期lot时拒绝，需先release再reserve。
- ship: {kind:'ship',order_id,items:[{sku,quantity}]}。items非空、sku不重复且属于订单。订单不能到期、数量不能超过已预占；按allocations现有顺序消耗，减少lot.on_hand/reserved与allocation，移除零allocation，增加line.shipped及sale账目，更新派生status。若本次实际消耗的lot已到期则拒绝整批。
- release: {kind:'release',order_id}。释放全部预占，保留未履约数量与shipped/cancelled；更新status。无预占时允许。
- cancel: {kind:'cancel',order_id}。释放全部预占，将所有未发货数量计入cancelled（保留已发货），更新status。重复取消允许。
- expire: {kind:'expire'}。对所有expires_at<=now的订单执行cancel，其他订单不变；lot本身不会被删除，过期库存仍算on_hand但不可供货。

## 查询、金额和业务日志
supply.queries.stock(state,query)：query只允许sku/warehouse/available_only；available_only必须bool。返回各lot加available字段：到期lot为0，其他为on_hand-reserved。available_only=true过滤available=0；按(warehouse,sku,expires_at升序且null最后,id)排序。
supply.queries.orders(state,query)：只允许status/warehouse/sku/offset/limit；前三者精确筛选，按id升序，offset>=0，limit=null或>=0，均非bool；limit=0返回[]。sku筛选只筛订单，不裁掉订单其他lines。
supply.finance.revenue(state,query)使用与orders完全相同的筛选及分页，按选中的全部lines返回{gross,refunded,net,shipped_units,returned_units}：gross=sum(shipped*unit_price)，refunded=sum(returned*unit_price)，net=gross-refunded。目前returned为0，后续追加规则不得改这些定义。
supply.journal.changes_since(state,since) 返回revision>since的event深复制，since非负整数非bool。

## 文件与CLI
repository.load(path)只读解析并migrate，不写盘。repository.save(state,path)先validate，再同目录临时文件写UTF-8 JSON、flush/fsync、os.replace原子提交，失败保留原文件并清理临时文件。
python -m supply.cli DATABASE COMMAND；所有INPUT均为UTF-8 JSON文件路径，不是内联JSON。receive INPUT、report保留旧命令；migrate保存迁移结果，仅在原schema不是2时写盘；preview ENVELOPE、apply ENVELOPE、stock QUERY、orders QUERY、revenue QUERY、audit SINCE。成功stdout恰一行JSON、stderr为空、exit=0；业务/数据/OSError失败exit=2，stderr非空、stdout为空，数据库字节不变。preview/query/report/audit不写盘；apply回放不写盘，连inode和mtime都不能改变。新增需求到达后再实现verify/rebuild，原命令保持。

必须交付供项目使用的测试及README示例：python -m supply.cli 的 preview/apply/stock/orders/revenue/verify/rebuild，及python -m unittest discover -s tests -v。只允许修改supply/、tests/、README.md；不要修改公开检查及data样例。
'''

CHANGE = '''新增退货退款和日志恢复，必须保留已经完成的库存、预占、发货、版本、幂等、查询和旧CLI行为。
1. 新action恰为{kind:'return',order_id,warehouse,items:[{sku,quantity,lot_id}]}。warehouse与标识规范，items非空且sku唯一、属于订单，quantity正整数非bool，lot_id全局尚不存在。每项只能退shipped-returned数量；新lot的sku为原sku，warehouse取指定退货仓，expires_at=null，on_hand=退量，reserved=0。增加line.returned，增加kind=refund账目，其quantity/unit_price/amount使用原订单价格；不能自行改价。整批失败不产生任何库存或退款。退货不改shipped/cancelled/allocations和派生履约status，不重新打开订单。退款金额按整数分，gross保持历史发货收入，refunded按累计退货金额，net=gross-refunded，库存/查询/金额/日志一致；request_id回放保持所有对象、日志和文件不变。
2. supply.journal.rebuild(document)只信genesis与events，可以从错误lots/orders/ledger/head等投影恢复。验证genesis是reserved=0的规范lot字典，从revision=clock=0的空订单/账目状态开始，按event逐条构造envelope并重放全部actions（含退货）。每条event的字段必须准确，revision从1连续，request_id不能重复，时间不倒退，previous连接前hash；hash必须等于公开SHA256公式。非法日志/业务操作抛ValueError，绝不跳过或修补事件。返回完整schema2规范状态，完整event序列和genesis不变。
3. supply.journal.verify(state)先validate，再将当前状态与rebuild(state)整对象比较；一致返回True，任意投影差异或日志篡改抛ValueError，不修改输入。
4. CLI verify只读，成功返回{valid:true,revision,head}。CLI rebuild必须直接读原JSON（不能先load/validate错误投影），恢复并原子保存，输出完整状态。日志非法时exit=2且文件/临时文件不变。重复verify不能改inode/mtime；旧migrate/report及apply回放的只读要求仍保留。README补全退货及恢复示例，并为跨仓、部分发货、退款、日志篡改/恢复和失败原子性补充测试。
'''

def write(path, source):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(textwrap.dedent(source),encoding='utf-8')


def prepare(package=PACKAGE):
    package=inside_project(package)
    if package.exists():
        raise ValueError('题包已存在，不覆盖历史版本')
    spec=importlib.util.spec_from_file_location('highchain_checks',Path(__file__).with_name('highchain_checks.py'))
    checks=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checks)
    LEGACY_TESTS=checks.LEGACY_TESTS
    PUBLIC_LEGACY=checks.PUBLIC_LEGACY
    PUBLIC_INITIAL=checks.PUBLIC_INITIAL
    PUBLIC_CHANGE=checks.PUBLIC_CHANGE
    GRADE=checks.GRADE
    REFERENCE_TESTS=checks.REFERENCE_TESTS
    signatures={'schema':{'validate':'state'},'migration':{'migrate':'raw'},
        'planner':{'preview':'state,envelope','apply_batch':'state,envelope'},
        'queries':{'stock':'state,query','orders':'state,query','report':'state'},
        'finance':{'revenue':'state,query'},'journal':{'changes_since':'state,since','verify':'state','rebuild':'document'}}
    for name,functions in signatures.items():
        stub='\n\n'.join(f'def {fn}({args}):\n    """Implement the public contract, preserving input objects."""\n    raise NotImplementedError' for fn,args in functions.items())+'\n'
        write(package/'initial/supply'/f'{name}.py',stub)
        write(package/'reference/supply'/f'{name}.py','from .core import '+','.join(functions)+'\n')
    for stage in ('initial','reference'):
        write(package/stage/'supply/__init__.py','')
        write(package/stage/'supply/legacy.py',LEGACY)
        write(package/stage/'supply/codec.py',CODEC)
        write(package/stage/'tests/test_legacy.py',LEGACY_TESTS)
    write(package/'initial/supply/repository.py',INITIAL_REPOSITORY)
    write(package/'initial/supply/cli.py',INITIAL_CLI)
    write(package/'reference/supply/core.py',CORE)
    write(package/'reference/supply/repository.py',REPOSITORY)
    write(package/'reference/supply/cli.py',CLI)
    write(package/'reference/tests/test_fulfillment.py',REFERENCE_TESTS)
    write(package/'initial/README.md',DESCRIPTION)
    write(package/'reference/README.md',DESCRIPTION+'\n'+CHANGE)
    for name,source in [('public_legacy.py',PUBLIC_LEGACY),('public_initial.py',PUBLIC_INITIAL),('public_change.py',PUBLIC_CHANGE)]:
        write(package/'initial'/name,source)
    for name,example in [('legacy0.json',{'schema':0,'rows':[{'sku':' A ','quantity':6},{'sku':'B','quantity':4}]}),
                         ('legacy1.json',{'schema':1,'stocks':{'A':6,'B':4}})]:
        write(package/'initial/data'/name,json.dumps(example,ensure_ascii=False,indent=2))
    python=str(PROJECT/'.venv/bin/python')
    task=Task(id='H1-v1',objective='升级已有库存工具，交付多仓订单履约：迁移旧数据、FEFO库存预占、部分发货与过期取消、调拨、批次事务与幂等、金额对账和可追溯日志，保留旧API/CLI并补充项目测试与说明。',
        constraints=['只使用Python标准库，不访问公网，不修改公开检查、固定样例或评分器。','文件输入、精确字段、时间/版本及整数分金额规则按README执行；保持跨模块一致性。'],
        acceptance=['数量、预占与金额一致；整批失败无副作用，深复制和只读行为正确；旧接口和新增链路均可运行。'],
        write_paths=['supply/','tests/','README.md'],
        protected_paths=['public_legacy.py','public_initial.py','public_change.py','data/legacy0.json','data/legacy1.json'],
        checks=[{'id':'legacy','argv':[python,'public_legacy.py'],'timeout':25},
                {'id':'initial','argv':[python,'public_initial.py'],'timeout':25}],
        feedback={'probe':{'id':'fulfillment_probe','argv':[python,'public_initial.py','--probe'],'timeout':20},
            'text':CHANGE,'acceptance':['退货数量和原价退款守恒，恢复与校验一致，旧履约链保持，公开追加检查通过。'],
            'checks':[{'id':'change','argv':[python,'public_change.py'],'timeout':25}]})
    write(package/'task.json',task.model_dump_json(indent=2))
    write(package/'description.md',DESCRIPTION+'\n## 中途追加要求\n'+CHANGE)
    write(package/'grade.py',GRADE)
    write(package/'contract.json',json.dumps({'version':'H1-v1','origin':'fresh authored source/reference; no historical model answers',
        'classification':'high technical and integration complexity; long-chain candidate','human_duration_calibrated':False,
        'feedback_trigger':'first successful fulfillment_probe (FEFO reserve plus partial shipment)',
        'time_seconds_per_group':1800,'cumulative_token_limit':None,'feedback_text':CHANGE},ensure_ascii=False,indent=2))
    return package


if __name__=='__main__':
    print(prepare())
