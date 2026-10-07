"""Independent, fixed-value business checks for the freshly authored H1 task."""

COMMON = r'''import copy,json,os,re,subprocess,sys,tempfile
from pathlib import Path
from hashlib import sha256
from unittest.mock import patch
sys.path.insert(0,os.getcwd())
from supply import legacy,migration,schema,planner,queries,finance,journal,repository

def same(a,b):assert a==b,(a,b)
def rejects(fn,*args):
    try:fn(*args)
    except ValueError:return
    raise AssertionError('must reject invalid input')
def empty():return migration.migrate({'schema':1,'stocks':{}})
def env(s,actions,now=1,key=None):return dict(request_id=key or 'batch:'+str(s['revision']),expected_revision=s['revision'],now=now,actions=actions)
def run(s,actions,now=1,key=None):return planner.apply_batch(s,env(s,actions,now,key))
def receipt(key,qty=6,expiry=None,warehouse='main',sku='A'):
    return dict(kind='receive',lot_id=key,sku=sku,warehouse=warehouse,quantity=qty,expires_at=expiry)
def order(key='O',qty=5,price=199,warehouse='main',expiry=100,sku='A'):
    return dict(kind='order',id=key,warehouse=warehouse,expires_at=expiry,lines=[dict(sku=sku,quantity=qty,unit_price=price)])
def reserve(key='O'):return dict(kind='reserve',order_id=key)
def ship(qty=2,key='O',sku='A'):return dict(kind='ship',order_id=key,items=[dict(sku=sku,quantity=qty)])
def returned(qty=1,key='O',lot='R',sku='A'):
    return dict(kind='return',order_id=key,warehouse='returns',items=[dict(sku=sku,quantity=qty,lot_id=lot)])
def held():return run(empty(),[receipt('L'),order(),reserve()])
def shipped():return run(held(),[ship()],2)
def invoke(db,*args):return subprocess.run([sys.executable,'-B','-m','supply.cli',str(db),*args],capture_output=True,text=True,timeout=12)
def good(result):
    assert result.returncode==0 and not result.stderr and len(result.stdout.splitlines())==1,(result.returncode,result.stderr,result.stdout[:250])
    return json.loads(result.stdout)
def bad(result,db,before):
    assert result.returncode==2 and result.stderr and not result.stdout,(result.returncode,result.stderr,result.stdout[:250])
    same(db.read_bytes(),before)
def file(directory,name,value):
    p=Path(directory)/name;p.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');return p
def stamp(p):return p.read_bytes(),p.stat().st_ino,p.stat().st_mtime_ns
'''

# Each case stands alone, uses public APIs and spec-derived values, and is independent
# of the reference implementation. Only selected cases are embedded in public checks.
CASES = {
'legacy_api': ('保留旧收货和汇总函数', r'''
def legacy_api():
    s={'schema':1,'stocks':{'B':0,'A':4}};before=copy.deepcopy(s)
    n=legacy.receive(s,{'sku':' A ','quantity':2});same(s,before)
    same(legacy.report(n),{'units':6,'skus':{'A':6,'B':0}})
    for qty in (True,0,-1,1.5):rejects(legacy.receive,s,{'sku':'A','quantity':qty})
    n['stocks']['A']=99;same(s,before)
'''),
'legacy_cli': ('保留旧CLI及库存汇总形状', r'''
def legacy_cli():
    with tempfile.TemporaryDirectory() as d:
        db=file(d,'db.json',{'schema':1,'stocks':{'B':0,'A':4}});row=file(d,'receipt.json',{'sku':' A ','quantity':2})
        same(good(invoke(db,'report')),{'units':4,'skus':{'A':4,'B':0}})
        good(invoke(db,'receive',str(row)))
        same(good(invoke(db,'report')),{'units':6,'skus':{'A':6,'B':0}})
'''),
'migration_zero': ('数组旧数据迁移及开账库存隔离', r'''
def migration_zero():
    raw={'schema':0,'rows':[{'sku':' A ','quantity':7},{'sku':'B','quantity':0}]};before=copy.deepcopy(raw)
    s=migration.migrate(raw);same(raw,before)
    same(s['lots']['legacy:A'],dict(id='legacy:A',sku='A',warehouse='main',expires_at=None,on_hand=7,reserved=0))
    same(s['genesis'],s['lots']);same((s['revision'],s['clock'],s['orders'],s['events']),(0,0,{},[]))
    s['lots']['legacy:A']['on_hand']=0;assert s['genesis']['legacy:A']['on_hand']==7
'''),
'migration_one': ('字典旧数据、碰撞拒绝和迁移幂等', r'''
def migration_one():
    raw={'schema':1,'stocks':{' A ':3,'B':2}};s=migration.migrate(raw)
    same(migration.migrate(s),s);same(queries.report(s),{'units':5,'skus':{'A':3,'B':2}})
    for badraw in ({'schema':1,'stocks':{' A ':2,'A':3}},{'schema':0,'rows':[{'sku':'A','quantity':1},{'sku':' A ','quantity':2}]},{'schema':1,'stocks':{'A':True}},{'schema':True,'stocks':{}},{'schema':1,'stocks':{' ':0}}):rejects(migration.migrate,badraw)
'''),
'strict_schema': ('规范字段、整数类型和订单派生状态', r'''
def strict_schema():
    s=held()
    for path,value in [(('revision',),True),(('clock',),True),(('schema',),True),(('lots','L','on_hand'),True),(('orders','O','lines','A','unit_price'),True),(('orders','O','status'),'open')]:
        n=copy.deepcopy(s);target=n
        for key in path[:-1]:target=target[key]
        target[path[-1]]=value;rejects(schema.validate,n)
    n=empty();n['extra']=1;rejects(schema.validate,n)
    n=held();n['lots']['L']['sku']=' A ';rejects(schema.validate,n)
'''),
'conservation': ('订单预占和lot预占数量严格守恒', r'''
def conservation():
    s=held();same(s['lots']['L']['reserved'],5)
    n=copy.deepcopy(s);n['lots']['L']['reserved']=4;rejects(schema.validate,n)
    n=copy.deepcopy(s);n['orders']['O']['lines']['A']['allocations'][0]['quantity']=6;n['lots']['L']['reserved']=6;rejects(schema.validate,n)
    n=copy.deepcopy(s);n['orders']['O']['lines']['A']['allocations'].append({'lot_id':'L','quantity':1});rejects(schema.validate,n)
'''),
'fefo': ('按到期时间及ID预占且不跨仓', r'''
def fefo():
    s=run(empty(),[receipt('Z',2,10),receipt('A',2,10),receipt('never',9),receipt('early',2,8),receipt('other',50,5,'remote'),order(qty=7),reserve()])
    same(s['orders']['O']['lines']['A']['allocations'],[{'lot_id':'early','quantity':2},{'lot_id':'A','quantity':2},{'lot_id':'Z','quantity':2},{'lot_id':'never','quantity':1}])
    same(s['lots']['other']['reserved'],0)
    n=run(s,[reserve()],2);same(n['lots'],s['lots']);same(n['orders'],s['orders'])
'''),
'shortage_atomic': ('多商品不足时整批回滚', r'''
def shortage_atomic():
    s=run(empty(),[receipt('L'),{**order(),'lines':[dict(sku='A',quantity=4,unit_price=3),dict(sku='B',quantity=1,unit_price=9)]}]);before=copy.deepcopy(s)
    rejects(planner.apply_batch,s,env(s,[receipt('temp',2,sku='C'),reserve()],2));same(s,before)
'''),
'expired_stock': ('过期库存不可供货且预占需先释放', r'''
def expired_stock():
    s=run(empty(),[receipt('early',3,5),receipt('late',5,50),order(qty=4),reserve()])
    before=copy.deepcopy(s);rejects(planner.apply_batch,s,env(s,[ship(1)],5));rejects(planner.apply_batch,s,env(s,[reserve()],5));same(s,before)
    n=run(s,[dict(kind='release',order_id='O'),reserve()],5)
    same(n['orders']['O']['lines']['A']['allocations'],[{'lot_id':'late','quantity':4}])
    assert next(x for x in queries.stock(n,{}) if x['id']=='early')['available']==0
'''),
'partial_ship': ('跨批次分配的部分发货及数量变化', r'''
def partial_ship():
    s=run(empty(),[receipt('early',2,10),receipt('late',5,20),order(),reserve(),ship(3)])
    same((s['lots']['early']['on_hand'],s['lots']['early']['reserved']),(0,0))
    same((s['lots']['late']['on_hand'],s['lots']['late']['reserved']),(4,2))
    same(s['orders']['O']['status'],'partial');same(s['orders']['O']['lines']['A']['allocations'],[{'lot_id':'late','quantity':2}])
    n=run(s,[ship(2)],2);same(n['orders']['O']['status'],'fulfilled');same(n['lots']['late']['reserved'],0)
'''),
'overship_atomic': ('超预占发货拒绝且整批无副作用', r'''
def overship_atomic():
    s=held();before=copy.deepcopy(s)
    rejects(planner.apply_batch,s,env(s,[ship(2),ship(4)],2));same(s,before)
    for action in (ship(6),ship(True),dict(kind='ship',order_id='O',items=[{'sku':'A','quantity':1},{'sku':'A','quantity':1}])):rejects(planner.apply_batch,s,env(s,[action],2))
'''),
'transfer': ('跨仓调拨只移动未预占库存', r'''
def transfer():
    s=held();a=dict(kind='transfer',lot_id='L',quantity=1,warehouse='east',new_lot_id='E')
    n=run(s,[a],2);same((n['lots']['L']['on_hand'],n['lots']['L']['reserved']),(5,5));same(n['lots']['E'],dict(id='E',sku='A',warehouse='east',expires_at=None,on_hand=1,reserved=0))
    for bad_a in ({**a,'quantity':2},{**a,'warehouse':'main'},{**a,'new_lot_id':'L'}):rejects(planner.apply_batch,s,env(s,[bad_a],2))
    n=run(empty(),[receipt('X',2,5)]);m=run(n,[{**a,'lot_id':'X'}],2);same(m['lots']['E']['expires_at'],5)
    rejects(planner.apply_batch,n,env(n,[{**a,'lot_id':'X'}],5))
'''),
'cancel_release': ('释放保留履约数量且取消保留已发货', r'''
def cancel_release():
    s=shipped();n=run(s,[dict(kind='release',order_id='O')],3)
    same(n['orders']['O']['status'],'partial');same(n['orders']['O']['lines']['A']['shipped'],2);same(n['lots']['L']['reserved'],0)
    n=run(n,[reserve(),dict(kind='cancel',order_id='O')],4)
    same((n['orders']['O']['status'],n['orders']['O']['lines']['A']['cancelled'],n['lots']['L']['reserved']),('closed',3,0))
    same(run(n,[dict(kind='cancel',order_id='O')],5)['orders'],n['orders'])
'''),
'expiry_boundary': ('到期边界取消与未到期订单隔离', r'''
def expiry_boundary():
    s=run(empty(),[receipt('L',10),order('O',3,expiry=5),reserve(),order('P',2,expiry=6),reserve('P')])
    rejects(planner.apply_batch,s,env(s,[ship(1)],5))
    n=run(s,[{'kind':'expire'}],5);same(n['orders']['O']['status'],'cancelled');same(n['orders']['P']['status'],'reserved');same(n['lots']['L']['reserved'],2)
    assert run(n,[{'kind':'expire'}],6)['lots']['L']['reserved']==0
'''),
'dependent_batch': ('同批收货调拨预占发货的依赖链', r'''
def dependent_batch():
    s=run(empty(),[receipt('L',7,50),dict(kind='transfer',lot_id='L',quantity=4,warehouse='east',new_lot_id='E'),order(warehouse='east',qty=4),reserve(),ship(4)])
    same((s['revision'],len(s['events']),s['clock']),(1,1,1));same(s['orders']['O']['status'],'fulfilled');same(s['lots']['L']['on_hand'],3);same(s['lots']['E']['on_hand'],0)
'''),
'revision_clock': ('版本冲突、时间倒退及空批次', r'''
def revision_clock():
    s=held();p=env(s,[],3)
    for wrong in ({**p,'expected_revision':0},{**p,'expected_revision':True},{**p,'now':0},{**p,'now':True},{**p,'other':1}):rejects(planner.apply_batch,s,wrong)
    n=run(s,[],3);same((n['revision'],n['clock'],len(n['events'])),(2,3,2))
'''),
'early_replay': ('重复批次忽略错误载荷且不重复业务', r'''
def early_replay():
    s=shipped();p={'request_id':s['batches'][0],'now':False,'expected_revision':-99,'actions':None,'extra':1}
    n=planner.apply_batch(s,p);same(n,s);n['lots']['L']['on_hand']=999;assert s['lots']['L']['on_hand']==4
    same(planner.preview(s,p),dict(request_id=s['batches'][0],base_revision=2,next_revision=2,changed_lots=[],changed_orders=[],gross_delta=0,refund_delta=0,replayed=True))
    rejects(planner.apply_batch,s,{'request_id':' '+s['batches'][0]})
'''),
'preview': ('预览与提交一致并保持输入不变', r'''
def preview_case():
    s=held();p=env(s,[ship(2)],2);before=copy.deepcopy((s,p));out=planner.preview(s,p)
    same((s,p),before);same(out,dict(request_id='batch:1',base_revision=1,next_revision=2,changed_lots=['L'],changed_orders=['O'],gross_delta=398,refund_delta=0,replayed=False))
    n=planner.apply_batch(s,p);same(n['orders']['O']['lines']['A']['shipped'],2);same((s,p),before)
'''),
'stock_query': ('库存筛选排序、过期可用量与零库存', r'''
def stock_query():
    s=run(empty(),[receipt('Z',2,5),receipt('A',3,5),receipt('N',1),receipt('E',4,warehouse='east'),order(qty=2),reserve()]);s=run(s,[],5)
    rows=queries.stock(s,{'warehouse':'main'});same([x['id'] for x in rows],['A','Z','N']);same([x['available'] for x in rows],[0,0,1])
    same([x['id'] for x in queries.stock(s,{'available_only':True})],['E','N'])
    rejects(queries.stock,s,{'available_only':1});rejects(queries.stock,s,{'other':1})
'''),
'orders_query': ('订单筛选分页与完整商品行', r'''
def orders_query():
    multi={**order('B'), 'lines':[dict(sku='A',quantity=1,unit_price=3),dict(sku='B',quantity=2,unit_price=4)]}
    s=run(empty(),[multi,order('A',warehouse='east'),order('C')])
    same([x['id'] for x in queries.orders(s,{'sku':'A','offset':1,'limit':1})],['B'])
    same(set(queries.orders(s,{'sku':'B'})[0]['lines']),{'A','B'});same(queries.orders(s,{'limit':0}),[])
    for q in ({'offset':True},{'limit':-1},{'status':'bad'},{'warehouse':' main '}):rejects(queries.orders,s,q)
'''),
'integer_revenue': ('原价整数分收入、账目及筛选口径', r'''
def integer_revenue():
    actions=[receipt('A',10),receipt('B',10,sku='B'),{**order('O'),'lines':[dict(sku='A',quantity=3,unit_price=9007199254740993),dict(sku='B',quantity=2,unit_price=0)]},reserve(),dict(kind='ship',order_id='O',items=[dict(sku='A',quantity=2),dict(sku='B',quantity=1)])]
    s=run(empty(),actions);same(finance.revenue(s,{'sku':'B'}),dict(gross=18014398509481986,refunded=0,net=18014398509481986,shipped_units=3,returned_units=0))
    same([e['amount'] for e in s['ledger']],[18014398509481986,0]);same([e['request_id'] for e in s['ledger']],['batch:0','batch:0'])
'''),
'atomic_persistence': ('原子保存失败保留文件并清理临时文件', r'''
def atomic_persistence():
    with tempfile.TemporaryDirectory() as d:
        db=file(d,'db.json',empty());before=stamp(db);names=set(Path(d).iterdir())
        with patch('os.replace',side_effect=OSError('offline forced failure')):
            try:repository.save(held(),db)
            except OSError:pass
            else:raise AssertionError('replace failure must propagate')
        same(stamp(db),before);same(set(Path(d).iterdir()),names)
        repository.save(held(),db);same(repository.load(db),held())
'''),
'readonly_load': ('读取迁移不写盘且返回对象独立', r'''
def readonly_load():
    with tempfile.TemporaryDirectory() as d:
        db=file(d,'db.json',{'schema':0,'rows':[dict(sku='A',quantity=3)]});before=stamp(db)
        s=repository.load(db);same(stamp(db),before);same(s['schema'],2)
        n=repository.load(db);s['lots']['legacy:A']['on_hand']=100;assert n['lots']['legacy:A']['on_hand']==3
        s['schema']=True;rejects(repository.save,s,db);same(stamp(db),before)
'''),
'cli_base': ('CLI文件输入、基础命令及失败原子性', r'''
def cli_base():
    with tempfile.TemporaryDirectory() as d:
        db=file(d,'db.json',empty());p=file(d,'plan.json',env(empty(),[receipt('L'),order(),reserve(),ship()],1));q=file(d,'query.json',{})
        before=stamp(db);preview=good(invoke(db,'preview',str(p)));same(stamp(db),before);same(preview['gross_delta'],398)
        s=good(invoke(db,'apply',str(p)));same(s['revision'],1);same(good(invoke(db,'stock',str(q)))[0]['on_hand'],4)
        same(good(invoke(db,'orders',str(q)))[0]['status'],'partial');same(good(invoke(db,'revenue',str(q)))['net'],398)
        same(len(good(invoke(db,'audit','0'))),1)
        before=db.read_bytes();bad(invoke(db,'apply',json.dumps(env(s,[]))),db,before)
        bad(invoke(db,'apply',str(file(d,'bad.json',env(s,[ship(9)],2)))),db,before)
'''),
'cli_readonly': ('查询、迁移回放的inode和mtime不变', r'''
def cli_readonly():
    with tempfile.TemporaryDirectory() as d:
        s=held();db=file(d,'db.json',s);q=file(d,'q.json',{});p=file(d,'replay.json',{'request_id':s['batches'][0],'actions':None});before=stamp(db)
        for args in [('migrate',),('report',),('stock',str(q)),('orders',str(q)),('revenue',str(q)),('audit','0'),('preview',str(p)),('apply',str(p))]:good(invoke(db,*args));same(stamp(db),before)
'''),
'return_partial': ('部分退货库存与原价退款同步', r'''
def return_partial():
    s=shipped();n=run(s,[returned()],3);line=n['orders']['O']['lines']['A']
    same((line['shipped'],line['returned'],line['cancelled']),(2,1,0));same(n['lots']['R'],dict(id='R',sku='A',warehouse='returns',expires_at=None,on_hand=1,reserved=0))
    same(finance.revenue(n,{}),dict(gross=398,refunded=199,net=199,shipped_units=2,returned_units=1));same(n['ledger'][-1]['kind'],'refund');same(n['ledger'][-1]['unit_price'],199)
    same(planner.preview(s,env(s,[returned()],3))['refund_delta'],199)
'''),
'overreturn_atomic': ('过量退货及重复lot拒绝且整批回滚', r'''
def overreturn_atomic():
    s=shipped();before=copy.deepcopy(s)
    for actions in ([returned(1),returned(2,lot='R2')],[returned(3)],[returned(True)],[returned(1,lot='L')],[{**returned(),'unit_price':1}]):rejects(planner.apply_batch,s,env(s,actions,3));same(s,before)
'''),
'return_status': ('退货不重开订单且保留预占和发货状态', r'''
def return_status():
    s=shipped();n=run(s,[returned()],3);same(n['orders']['O']['status'],'partial');same(n['orders']['O']['lines']['A']['allocations'],s['orders']['O']['lines']['A']['allocations'])
    s=run(s,[ship(3)],3);n=run(s,[returned(5)],4);same(n['orders']['O']['status'],'fulfilled');same(n['orders']['O']['lines']['A']['shipped'],5)
    same(finance.revenue(n,{}),dict(gross=995,refunded=995,net=0,shipped_units=5,returned_units=5))
'''),
'return_replay': ('退货回放不重复库存、退款或日志', r'''
def return_replay():
    s=shipped();p=env(s,[returned()],3,'refund');n=planner.apply_batch(s,p)
    same(planner.apply_batch(n,{'request_id':'refund','now':False}),n)
    same(len(n['ledger']),2);same(n['lots']['R']['on_hand'],1);same(n['revision'],3)
'''),
'journal_hash': ('业务日志规范哈希、连续版本与增量读取', r'''
def journal_hash():
    s=run(shipped(),[returned()],3);previous='0'*64
    for i,e in enumerate(s['events'],1):
        body={k:v for k,v in e.items() if k!='hash'};same(e['hash'],sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest());same(e['previous'],previous);same(e['revision'],i);previous=e['hash']
    same(s['head'],previous);same([e['revision'] for e in journal.changes_since(s,1)],[2,3]);rejects(journal.changes_since,s,True)
    rows=journal.changes_since(s,0);rows[0]['actions'].clear();assert s['events'][0]['actions']
'''),
'journal_recovery': ('损坏投影可从开账库存及日志完整恢复', r'''
def journal_recovery():
    s=run(shipped(),[returned()],3);damaged=copy.deepcopy(s);damaged.update(lots=None,orders={'fake':1},ledger=[],head='bad',revision=-1,clock=-1,schema=0)
    before=copy.deepcopy(damaged);same(journal.rebuild(damaged),s);same(damaged,before);same(journal.verify(s),True)
    n=copy.deepcopy(s);n['lots']['L']['on_hand']+=1;rejects(journal.verify,n)
    n=copy.deepcopy(s);n['ledger'][0]['unit_price']=1;n['ledger'][0]['amount']=2;rejects(journal.verify,n)
'''),
'journal_tamper': ('篡改、重排及非法操作日志均拒绝恢复', r'''
def journal_tamper():
    s=run(shipped(),[returned()],3)
    for kind in ('hash','action','previous','duplicate','time','revision','genesis'):
        n=copy.deepcopy(s)
        if kind=='hash':n['events'][0]['hash']='f'*64
        elif kind=='action':n['events'][0]['actions'][0]['quantity']=99
        elif kind=='previous':n['events'][1]['previous']='0'*64
        elif kind=='genesis':n['genesis']={'bad':dict(id='bad',sku='A',warehouse='main',expires_at=None,on_hand=2,reserved=1)}
        else:
            e=n['events'][1]
            if kind=='duplicate':e['request_id']=n['events'][0]['request_id']
            elif kind=='time':e['now']=0
            else:e['revision']=9
            body={k:v for k,v in e.items() if k!='hash'};e['hash']=sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        rejects(journal.rebuild,n);rejects(journal.verify,n)
'''),
'cli_recovery': ('CLI校验只读、恢复保存及非法日志不落盘', r'''
def cli_recovery():
    with tempfile.TemporaryDirectory() as d:
        s=run(shipped(),[returned()],3);db=file(d,'db.json',s);before=stamp(db)
        same(good(invoke(db,'verify')),dict(valid=True,revision=3,head=s['head']));same(stamp(db),before)
        damaged=copy.deepcopy(s);damaged.update(lots=None,orders=None,head='wrong');db.write_text(json.dumps(damaged));same(good(invoke(db,'rebuild')),s);same(json.loads(db.read_text()),s)
        n=copy.deepcopy(s);n['events'][0]['actions'][0]['quantity']=99;db.write_text(json.dumps(n));before=db.read_bytes();names=set(Path(d).iterdir());bad(invoke(db,'rebuild'),db,before);same(set(Path(d).iterdir()),names)
'''),
'nested_isolation': ('业务执行及查询返回值的嵌套对象隔离', r'''
def nested_isolation():
    s=held();p=env(s,[ship()],2);before=copy.deepcopy((s,p));n=planner.apply_batch(s,p);same((s,p),before)
    n['events'][1]['actions'][0]['items'][0]['quantity']=99;same(p['actions'][0]['items'][0]['quantity'],2)
    rows=queries.orders(s,{});rows[0]['lines']['A']['allocations'][0]['quantity']=99;same(s['lots']['L']['reserved'],5)
    n=schema.validate(s);n['genesis']['fake']={};assert 'fake' not in s['genesis']
'''),
'end_to_end': ('多仓调拨到退货再供货及恢复的完整链路', r'''
def end_to_end():
    s=migration.migrate({'schema':0,'rows':[dict(sku='A',quantity=12),dict(sku='B',quantity=8)]});original=copy.deepcopy(s['genesis'])
    s=run(s,[dict(kind='transfer',lot_id='legacy:A',quantity=7,warehouse='east',new_lot_id='EA'),dict(kind='transfer',lot_id='legacy:B',quantity=4,warehouse='east',new_lot_id='EB'),{**order(warehouse='east',qty=5),'lines':[dict(sku='A',quantity=5,unit_price=101),dict(sku='B',quantity=3,unit_price=203)]},reserve(),dict(kind='ship',order_id='O',items=[dict(sku='A',quantity=3),dict(sku='B',quantity=2)])],2)
    s=run(s,[returned(2),dict(kind='cancel',order_id='O'),order('N',qty=2,price=80,warehouse='returns'),reserve('N'),ship(2,'N')],3)
    same(finance.revenue(s,{}),dict(gross=869,refunded=202,net=667,shipped_units=7,returned_units=2));same(s['orders']['O']['status'],'closed');same(s['orders']['N']['status'],'fulfilled')
    same(sum(x['on_hand'] for x in s['lots'].values()),15);same(sum(x['reserved'] for x in s['lots'].values()),0);same(s['genesis'],original);same(journal.rebuild(s),s);assert journal.verify(s)
'''),
'tests_docs': ('项目测试可运行及业务使用说明齐全', r'''
def tests_docs():
    result=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-v'],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,(result.stdout,result.stderr)
    m=re.search(r'Ran (\d+) tests',result.stderr);assert m and int(m[1])>=8,result.stderr
    text=Path('README.md').read_text(encoding='utf-8')
    for phrase in ('python -m supply.cli','preview','apply','stock','orders','revenue','verify','rebuild','return','python -m unittest discover -s tests -v'):assert phrase in text,phrase
'''),
}


def source(keys):
    return COMMON + '\n'.join(CASES[key][1] for key in keys)


def names(key):
    return 'preview_case' if key == 'preview' else key


PUBLIC_INITIAL = source(['fefo','dependent_batch','preview','cancel_release','integer_revenue']) + r'''
if '--probe' in sys.argv:
    s=run(empty(),[receipt('early',2,10),receipt('late',5,20),order(),reserve(),ship(3)])
    same(s['orders']['O']['status'],'partial');same(s['lots']['early']['on_hand'],0);same(s['lots']['late']['reserved'],2)
else:
    fefo();dependent_batch();preview_case();cancel_release();integer_revenue()
print('initial checks passed')
'''

PUBLIC_CHANGE = source(['return_partial','return_status','journal_hash','journal_recovery','journal_tamper']) + r'''
return_partial();return_status();journal_hash();journal_recovery();journal_tamper()
print('change checks passed')
'''

PUBLIC_LEGACY = r'''import copy,json,subprocess,sys,tempfile
from pathlib import Path
from supply import legacy
s={'schema':1,'stocks':{'A':4,'B':0}};before=copy.deepcopy(s)
n=legacy.receive(s,{'sku':' A ','quantity':2});assert s==before and legacy.report(n)=={'units':6,'skus':{'A':6,'B':0}}
with tempfile.TemporaryDirectory() as d:
    db=Path(d)/'db.json';row=Path(d)/'row.json';db.write_text(json.dumps(s));row.write_text(json.dumps({'sku':' A ','quantity':2}))
    for args,expected in [(('receive',str(row)),None),(('report',),{'units':6,'skus':{'A':6,'B':0}})]:
        r=subprocess.run([sys.executable,'-B','-m','supply.cli',str(db),*args],capture_output=True,text=True,timeout=10)
        assert r.returncode==0 and not r.stderr and len(r.stdout.splitlines())==1,(r.returncode,r.stderr)
        if expected is not None:assert json.loads(r.stdout)==expected
print('legacy checks passed')
'''

GRADE = source(CASES) + '\nchecks=[]\n' + r'''
def check(key,label,fn):
    try:fn();checks.append(dict(id=key,label=label,passed=True))
    except Exception as error:checks.append(dict(id=key,label=label,passed=False,error=type(error).__name__+':'+str(error)))
''' + '\n'.join(f'check({key!r},{label!r},{names(key)})' for key,(label,_) in CASES.items()) + r'''
passed=all(c['passed'] for c in checks)
print(json.dumps(dict(passed=passed,checks=checks),ensure_ascii=False))
raise SystemExit(0 if passed else 1)
'''

LEGACY_TESTS = r'''import unittest
from supply import legacy

class LegacyTests(unittest.TestCase):
    def test_receive(self):
        s={'schema':1,'stocks':{'A':2}};n=legacy.receive(s,{'sku':' A ','quantity':1});self.assertEqual(n['stocks']['A'],3);self.assertEqual(s['stocks']['A'],2)
    def test_report(self):self.assertEqual(legacy.report({'schema':1,'stocks':{'B':0,'A':2}}),{'units':2,'skus':{'A':2,'B':0}})
    def test_bool_rejected(self):
        with self.assertRaises(ValueError):legacy.receive({'schema':1,'stocks':{}},{'sku':'A','quantity':True})
    def test_blank_rejected(self):
        with self.assertRaises(ValueError):legacy.receive({'schema':1,'stocks':{}},{'sku':' ','quantity':2})
'''

REFERENCE_TESTS = source(['fefo','partial_ship','transfer','shortage_atomic','return_partial','journal_recovery','journal_tamper','atomic_persistence','end_to_end']) + r'''
import unittest
class FulfillmentTests(unittest.TestCase):
    def test_fefo(self):fefo()
    def test_partial_ship(self):partial_ship()
    def test_transfer(self):transfer()
    def test_atomic_shortage(self):shortage_atomic()
    def test_return(self):return_partial()
    def test_recovery(self):journal_recovery()
    def test_tamper(self):journal_tamper()
    def test_atomic_save(self):atomic_persistence()
    def test_full_chain(self):end_to_end()
'''
