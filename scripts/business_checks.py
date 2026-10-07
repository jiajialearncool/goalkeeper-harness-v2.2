"""Fixed business expectations, independent of the reference implementation."""

COMMON = r"""import copy,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,os.getcwd())
from PACKAGE import core,util
def same(a,b):assert a==b,(a,b)
def rejects(fn,*args):
    try:fn(*args)
    except ValueError:return
    raise AssertionError('must raise ValueError')
def legacy():
    rows=[{'id':'A','value':2},{'id':'B','value':0}];before=copy.deepcopy(rows)
    same(util.legacy_summary(rows),{'count':2,'total':2});same(rows,before)
    rejects(util.legacy_summary,[{'id':'A','value':True}])
def cli(command,p,save=False):
    with tempfile.TemporaryDirectory() as d:
        f=Path(d)/'input.json';f.write_text(json.dumps(p));before=f.read_bytes();out=Path(d)/'out.json'
        args=[sys.executable,'-B','-m','PACKAGE.cli',command,str(f)]
        if save:args+=['--save',str(out)]
        r=subprocess.run(args,capture_output=True,text=True,timeout=15)
        assert r.returncode==0 and not r.stderr and len(r.stdout.splitlines())==1,(r.returncode,r.stderr)
        value=json.loads(r.stdout);same(f.read_bytes(),before)
        if save:same(json.loads(out.read_text()),value)
        return value
def cli_invalid():
    with tempfile.TemporaryDirectory() as d:
        out=Path(d)/'out.json';out.write_text('KEEP')
        r=subprocess.run([sys.executable,'-B','-m','PACKAGE.cli','summary','[{"id":"A","value":2}]','--save',str(out)],capture_output=True,text=True,timeout=15)
        assert r.returncode==2 and r.stderr and not r.stdout
        same(out.read_text(),'KEEP')
def atomic_save():
    with tempfile.TemporaryDirectory() as d:
        out=Path(d)/'out.json';out.write_text('KEEP');before=set(Path(d).iterdir())
        with patch('os.replace',side_effect=OSError('forced')):
            try:util.save({'new':1},out)
            except OSError:pass
            else:raise AssertionError('must propagate write failure')
        same(out.read_text(),'KEEP');same(set(Path(d).iterdir()),before)
def docs_tests():
    assert Path('README.md').is_file() and '-m PACKAGE.cli' in Path('README.md').read_text()
    tests=list(Path('tests').glob('test*.py'));assert len(tests)>=2,'add business tests'
    r=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-v'],capture_output=True,text=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
"""

BAD_COMMON = r"""
def data():
    return {'schema':1,'products':[{'sku':'A','category':'cat','spu':'s1'},{'sku':'B','category':'cat','spu':'s2'},{'sku':'C','category':'other','spu':'s1'}],
        'feedback':[{'id':'F','user':'U','sku':'A','at':10,'kind':'dislike'}],
        'requests':[{'id':'R','user':'U','at':20,'placement':'home','profile_ok':True,'joined':False,'trace':True,'items':[{'sku':'B','source':'organic','hit':False}]}]}
def query(sku='B',at=20):return {'user':'U','sku':sku,'at':at,'placement':'home'}
def v2(d):
    n=copy.deepcopy(d);n['schema']=2
    n['requests']=[{'request_id':r['id'],'user_id':r['user'],'timestamp':r['at'],'slot':r['placement'],'results':r['items'],'signals':{k:r[k] for k in ('profile_ok','joined','trace')}} for r in n['requests']]
    return n
"""
BAD = {
    "normalize": (
        "新旧日志统一与输入隔离",
        r"""d=data();before=copy.deepcopy(d);n=core.normalize(d);same(n,core.normalize(v2(d)));same(d,before);n['products']['A']['category']='x';same(d,before)""",
    ),
    "route": (
        "负反馈有效且链路未接入的归因",
        r"""r=core.diagnose(data(),query());same(r,{'reason':'route_not_integrated','request_id':'R','feedback_ids':['F'],'evidence':[{'source':'requests','id':'R'},{'source':'feedback','id':'F'}],'confirmed':True})""",
    ),
    "profile": (
        "画像不可用与未知状态分别处理",
        r"""d=data();d['requests'][0]['profile_ok']=False;same(core.diagnose(d,query())['reason'],'profile_unavailable');d['requests'][0]['profile_ok']=None;r=core.diagnose(d,query());same((r['reason'],r['confirmed']),('unknown_profile',False))""",
    ),
    "source": (
        "广告商品优先路由不套推荐结论",
        r"""d=data();d['requests'][0]['items'][0]['source']='ad';d['requests'][0]['trace']=False;same(core.diagnose(d,query())['reason'],'ad_route')""",
    ),
    "missing": (
        "兜底日志不足不能确定过滤器原因",
        r"""d=data();d['requests'][0]['trace']=False;r=core.diagnose(d,query());same((r['reason'],r['confirmed']),('insufficient_trace',False))""",
    ),
    "timing": (
        "反馈有效期边界和未来反馈排除",
        r"""d=data();d['feedback'][0]['at']=21;same(core.diagnose(d,query())['reason'],'no_active_feedback');d=data();at=10+180*86400;d['requests'][0]['at']=at;same(core.diagnose(d,query(at=at))['reason'],'no_active_feedback');d['requests'][0]['at']=at-1;same(core.diagnose(d,query(at=at-1))['reason'],'route_not_integrated')""",
    ),
    "relation": (
        "商品类目不匹配与缺失不可混为正常",
        r"""d=data();same(core.diagnose(d,query('C'))['reason'],'not_returned');d['requests'][0]['items'][0]['sku']='C';same(core.diagnose(d,query('C'))['reason'],'no_active_feedback');d['products'][2]['category']=None;r=core.diagnose(d,query('C'));same((r['reason'],r['confirmed']),('missing_product_relation',False))""",
    ),
    "signals": (
        "不一致日志和规则违规分开",
        r"""d=data();d['requests'][0]['joined']=True;same(core.diagnose(d,query())['reason'],'rule_violation');d['requests'][0]['items'][0]['hit']=True;r=core.diagnose(d,query());same((r['reason'],r['confirmed']),('inconsistent_trace',False))""",
    ),
    "request": (
        "精确请求关联与无日志和歧义",
        r"""d=data();same(core.diagnose(d,query(at=21))['reason'],'no_request');x=copy.deepcopy(d['requests'][0]);x['id']='R2';d['requests'].append(x);rejects(core.diagnose,d,query())""",
    ),
    "invalid": (
        "重复ID和非规范类型拒绝",
        r"""d=data();d['requests'][0]['at']=True;rejects(core.normalize,d);d=data();d['products'].append(copy.deepcopy(d['products'][0]));rejects(core.normalize,d);rejects(core.diagnose,data(),dict(query(),extra=1));d=data();d['requests'][0]['profile_ok']=1;rejects(core.normalize,d)""",
    ),
    "batch_compare": (
        "批量和前后版本逐项对应",
        r"""a=data();b=data();b['requests'][0]['joined']=True;same(core.compare(a,b,[query()]),[{'index':0,'before':'route_not_integrated','after':'rule_violation','changed':True}]);same(core.batch(a,[query(),query(at=21)])[1]['reason'],'no_request')""",
    ),
    "image": (
        "图片反馈按商品款式而非类目过滤",
        r"""d=data();d['feedback'][0]['kind']='image';same(core.diagnose(d,query())['reason'],'no_active_feedback');d['requests'][0]['items'][0]['sku']='C';same(core.diagnose(d,query('C'))['reason'],'route_not_integrated')""",
    ),
    "image_expiry": (
        "图片反馈有效期和原类目规则兼容",
        r"""d=data();d['feedback'][0]['kind']='image';d['requests'][0]['items'][0]['sku']='C';at=10+365*86400;d['requests'][0]['at']=at;same(core.diagnose(d,query('C',at))['reason'],'no_active_feedback');same(core.diagnose(data(),query())['reason'],'route_not_integrated')""",
    ),
    "cli": (
        "诊断CLI文件输入保存与旧命令",
        r"""same(cli('diagnose',{'data':v2(data()),'query':query()},True)['reason'],'route_not_integrated');same(cli('summary',[{'id':'A','value':3}]),{'count':1,'total':3})""",
    ),
}

EXP_COMMON = r"""
def data():
    assignments=[];events=[]
    counts=[('control','easy',2,2),('control','hard',8,4),('treatment','easy',8,7),('treatment','hard',2,0)]
    for g,c,n,k in counts:
        for i in range(n):
            u=g+c+str(i);assignments.append({'user':u,'group':g,'cohort':c,'version':'v','start':0,'end':100})
            events.append({'id':u,'user':u,'at':10,'version':'v','returned':True,'clicked':True,'converted':i<k})
    return {'schema':1,'assignments':assignments,'events':events}
def query(**kw):return dict(start=0,end=100,version='v',**kw)
def v2(d):
    n=copy.deepcopy(d);n['schema']=2;n['events']=[{'event_id':e['id'],'user_id':e['user'],'timestamp':e['at'],'strategy':e['version'],'stages':{k:e[k] for k in ('returned','clicked','converted')}} for e in n['events']];return n
"""
EXP = {
    "normalize": (
        "多版本日志与重复事件去重",
        r"""d=data();d['events'].append(copy.deepcopy(d['events'][0]));same(len(core.normalize(d)['events']),20);same(core.normalize(d),core.normalize(v2(d)))""",
    ),
    "aggregate": (
        "总体改善但分群都下降",
        r"""r=core.analyze(data(),query());same(r['groups']['control']['conversion_rate'],[3,5]);same(r['groups']['treatment']['conversion_rate'],[7,10]);same(r['harmed_cohorts'],['easy','hard']);same(r['cohorts']['treatment']['easy']['conversion_rate'],[7,8])""",
    ),
    "weighted": (
        "统一权重消除人群构成影响",
        r"""r=core.analyze(data(),query(weights={'easy':1,'hard':1}));same(r['standardized'],{'control':[3,4],'treatment':[7,16]})""",
    ),
    "funnel": (
        "漏斗分母与零分母规则",
        r"""d=data();d['events'][0].update(returned=False,clicked=False,converted=False);r=core.analyze(d,query());same(r['groups']['control']['ctr'],[1,1]);same(r['groups']['control']['conversion_rate'],[1,2]);same(core.analyze(d,dict(query(),start=50))['groups']['control']['ctr'],None)""",
    ),
    "excluded": (
        "未分流和版本不匹配单独计数",
        r"""d=data();e=copy.deepcopy(d['events'][0]);e.update(id='outside',user='unknown');d['events'].append(e);e=copy.deepcopy(d['events'][1]);e.update(id='wrong',version='old');d['events'].append(e);r=core.analyze(d,query());same(r['excluded'],{'unassigned':1,'version':1});same(len(r['evidence_ids']),20)""",
    ),
    "boundary": (
        "分析窗口与分流窗口左闭右开",
        r"""d=data();d['events'][0]['at']=100;d['events'][1]['at']=0;r=core.analyze(d,query());same(r['groups']['control']['requests'],9);assert d['events'][1]['id'] in r['evidence_ids']""",
    ),
    "overlap": (
        "分流重叠和冲突重复事件拒绝",
        r"""d=data();d['assignments'].append(copy.deepcopy(d['assignments'][0]));rejects(core.normalize,d);d=data();e=copy.deepcopy(d['events'][0]);e['converted']=False;d['events'].append(e);rejects(core.normalize,d)""",
    ),
    "invalid": (
        "非法漏斗、单位与权重拒绝",
        r"""d=data();d['events'][0].update(returned=False,clicked=True);rejects(core.normalize,d);rejects(core.analyze,data(),query(unit='unknown'));rejects(core.analyze,data(),query(weights={'easy':True}));rejects(core.analyze,data(),query(weights={'missing':1}));rejects(core.analyze,data(),dict(query(),start=True))""",
    ),
    "isolation": (
        "输入和结果嵌套对象隔离",
        r"""d=data();before=copy.deepcopy(d);r=core.analyze(d,query());r['groups']['control']['requests']=999;same(d,before);n=core.normalize(d);n['assignments'][0]['group']='wrong';same(d,before)""",
    ),
    "user": (
        "用户口径选择窗口内最后一次请求",
        r"""d=data();e=copy.deepcopy(d['events'][0]);e.update(id='LATEST',at=20,converted=False);d['events'].append(e);r=core.analyze(d,query(unit='user'));same(r['groups']['control']['requests'],10);same(r['groups']['control']['converted'],5);assert 'LATEST' in r['evidence_ids'] and d['events'][0]['id'] not in r['evidence_ids'];same(core.analyze(d,query())['groups']['control']['requests'],11)""",
    ),
    "tie": (
        "用户最新请求同时间按事件ID决定",
        r"""d=data();e=copy.deepcopy(d['events'][0]);e.update(id='zzzz',converted=False);d['events'].append(e);r=core.analyze(d,query(unit='user'));assert 'zzzz' in r['evidence_ids'];same(r['groups']['control']['converted'],5)""",
    ),
    "compare": (
        "新旧口径比较保留原有分析",
        r"""d=data();e=copy.deepcopy(d['events'][0]);e.update(id='LATEST',at=20,converted=False);d['events'].append(e);r=core.compare(d,query(),query(unit='user',weights={'easy':1,'hard':1}));same(r['before']['groups']['control']['requests'],11);same(r['after']['groups']['control']['requests'],10)""",
    ),
    "cli": (
        "分析CLI与标准化结果保存",
        r"""same(cli('analyze',{'data':v2(data()),'query':query(weights={'easy':1,'hard':1})},True)['harmed_cohorts'],['easy','hard']);same(cli('summary',[{'id':'A','value':4}]),{'count':1,'total':4})""",
    ),
}

RET_COMMON = r"""
def data():return {'revision':0,'features':{'A':{'depends':[],'status':'active','verified':False},'B':{'depends':['A'],'status':'active','verified':False},'C':{'depends':['A'],'status':'active','verified':False}},'consumers':{'biz':['B']},'data':{'A':[1,2],'B':[3],'C':[4]},'backup':{},'batches':[],'events':[]}
def env(s,actions,key=None):return {'request_id':key or 'b'+str(s['revision']),'expected_revision':s['revision'],'actions':actions}
def run(s,actions):return core.apply_batch(s,env(s,actions))
def stage(k):return [{'kind':x,'feature':k} for x in ('mark','clean','verify','finish')]
def detached():return run(data(),[{'kind':'detach','consumer':'biz'}])
"""
RET = {
    "plan": (
        "依赖闭包与下线顺序及引用阻止",
        r"""same(core.plan(data(),['A']),{'order':['B','C','A'],'blockers':['biz'],'safe':False});same(core.plan(detached(),['A'])['safe'],True)""",
    ),
    "cycle": (
        "循环依赖与未知目标拒绝",
        r"""d=data();d['features']['A']['depends']=['B'];rejects(core.validate,d);rejects(core.plan,data(),['Z']);rejects(core.plan,data(),['A','A'])""",
    ),
    "blocker": (
        "在用业务及依赖特征禁止下线",
        r"""rejects(core.apply_batch,data(),env(data(),stage('B')));s=detached();rejects(core.apply_batch,s,env(s,stage('A')))""",
    ),
    "chain": (
        "完整下线保留备份和状态一致",
        r"""s=run(detached(),stage('B')+stage('C')+stage('A'));same(core.reconcile(s),{'active':[],'retiring':[],'retired':['A','B','C'],'live_rows':0,'backup_rows':4,'revision':2});same(s['backup']['A'],[1,2])""",
    ),
    "phase": (
        "清理核对顺序不能跳过",
        r"""s=detached();rejects(core.apply_batch,s,env(s,[{'kind':'finish','feature':'B'}]));s=run(s,[{'kind':'mark','feature':'B'}]);rejects(core.apply_batch,s,env(s,[{'kind':'verify','feature':'B'}]));s=run(s,[{'kind':'clean','feature':'B'}]);rejects(core.apply_batch,s,env(s,[{'kind':'finish','feature':'B'}]))""",
    ),
    "atomic": (
        "下线批次失败输入不变",
        r"""s=detached();before=copy.deepcopy(s);rejects(core.apply_batch,s,env(s,stage('B')+[{'kind':'finish','feature':'Z'}]));same(s,before)""",
    ),
    "replay": (
        "重复批次忽略坏载荷且不重复日志",
        r"""s=detached();same(core.apply_batch(s,{'request_id':'b0','actions':None,'expected_revision':-99,'extra':1}),s);same(core.preview(s,{'request_id':'b0'}),{'base_revision':1,'next_revision':1,'changed':[],'replayed':True})""",
    ),
    "revision": (
        "版本冲突和非法动作拒绝",
        r"""s=data();rejects(core.apply_batch,s,dict(env(s,[]),expected_revision=True));rejects(core.apply_batch,s,dict(env(s,[]),expected_revision=1));rejects(core.apply_batch,s,env(s,[{'kind':'unknown','feature':'B'}]))""",
    ),
    "preview": (
        "预览与提交变化一致不改输入",
        r"""s=detached();p=env(s,stage('B'));before=copy.deepcopy((s,p));same(core.preview(s,p),{'base_revision':1,'next_revision':2,'changed':['B'],'replayed':False});same((s,p),before)""",
    ),
    "attach": (
        "新引用只允许仍然可用的特征",
        r"""s=run(detached(),stage('B'));rejects(core.apply_batch,s,env(s,[{'kind':'attach','consumer':'new','features':['B']}]));same(run(s,[{'kind':'attach','consumer':'new','features':['C']}])['consumers']['new'],['C'])""",
    ),
    "rollback": (
        "清理后恢复数据再接回业务",
        r"""s=run(detached(),[{'kind':'mark','feature':'B'},{'kind':'clean','feature':'B'}]);n=run(s,[{'kind':'rollback','feature':'B'},{'kind':'attach','consumer':'biz','features':['B']}]);same(n['data']['B'],[3]);same(n['features']['B'],{'depends':['A'],'status':'active','verified':False});same(n['backup'],{});same(core.plan(n,['A'])['blockers'],['biz'])""",
    ),
    "rollback_atomic": (
        "恢复后非法引用整批失败",
        r"""s=run(detached(),[{'kind':'mark','feature':'B'},{'kind':'clean','feature':'B'}]);before=copy.deepcopy(s);rejects(core.apply_batch,s,env(s,[{'kind':'rollback','feature':'B'},{'kind':'attach','consumer':'x','features':['Z']}]));same(s,before);s=run(s,[{'kind':'verify','feature':'B'},{'kind':'finish','feature':'B'}]);rejects(core.apply_batch,s,env(s,[{'kind':'rollback','feature':'B'}]))""",
    ),
    "isolation": (
        "验证和操作返回嵌套对象隔离",
        r"""d=data();n=core.validate(d);n['data']['A'].append(9);same(d['data']['A'],[1,2]);s=detached();n=core.apply_batch(s,{'request_id':'b0'});n['data']['A'].append(8);same(s['data']['A'],[1,2])""",
    ),
    "cli": (
        "计划和完整下线CLI及结果保存",
        r"""same(cli('plan',{'state':data(),'targets':['A']})['blockers'],['biz']);s=detached();n=cli('apply',{'state':s,'envelope':env(s,stage('B')+stage('C')+stage('A'))},True);same(cli('reconcile',n)['backup_rows'],4)""",
    ),
}

REC_COMMON = r"""
def data():return {'revision':0,'receipts':{},'wallets':{},'ledger':[],'batches':[],'events':[]}
def doc(price=1234):return {'merchant':' Shop ','date':'2026-10-01','number':' N1 ','items':[{'name':'item','quantity':2,'unit_price':price}]}
def submit(k='A',user='U',at=1,document=None):return {'kind':'submit','id':k,'user':user,'at':at,'document':document or doc()}
def review(k='A',version=0,status='approved',kind='review'):return {'kind':kind,'id':k,'expected_version':version,'status':status}
def env(s,actions,key=None):return {'request_id':key or 'b'+str(s['revision']),'expected_revision':s['revision'],'actions':actions}
def run(s,actions):return core.apply_batch(s,env(s,actions))
def approved():return run(data(),[submit(),review()])
"""
REC = {
    "document": (
        "OCR文档规范化和金额类型",
        r"""n=core.document(doc());same((n['merchant'],n['number']),('shop','n1'));d=doc();d['items'][0]['unit_price']=True;rejects(core.document,d);d=doc();d['items']=[];rejects(core.document,d)""",
    ),
    "fingerprint": (
        "同一小票指纹忽略OCR明细但区分票号",
        r"""a=doc();b=doc(10);b['merchant']='SHOP';b['number']='n1';same(core.fingerprint(a),core.fingerprint(b));b['number']='N2';assert core.fingerprint(a)!=core.fingerprint(b)""",
    ),
    "reward": (
        "审核后整数奖励和账目核对",
        r"""s=approved();same(core.reconcile(s),{'revision':1,'receipts':1,'approved':1,'rewarded':1,'total_reward':24,'wallets':{'U':24}});same(s['ledger'],[{'receipt_id':'A','user':'U','delta':24,'revision':1,'request_id':'b0'}])""",
    ),
    "duplicate": (
        "跨用户重复上传仅最早通过票获奖",
        r"""s=run(data(),[submit('B','V',2),review('B'),submit('A','U',1),review('A')]);same(s['wallets'],{'U':24,'V':0});same(s['receipts']['B']['reward'],0)""",
    ),
    "tie": (
        "同时间重复小票按ID决定归属",
        r"""s=run(data(),[submit('B','V'),review('B'),submit('A','U'),review('A')]);same(s['wallets'],{'U':24,'V':0})""",
    ),
    "correction": (
        "待审核OCR纠错及版本递增",
        r"""s=run(data(),[submit(),{'kind':'correct','id':'A','expected_version':0,'document':doc(500)},review(version=1)]);same(s['wallets'],{'U':10});same(s['receipts']['A']['version'],2);rejects(core.apply_batch,s,env(s,[{'kind':'correct','id':'A','expected_version':2,'document':doc()}]))""",
    ),
    "atomic": (
        "批次后半失败不产生奖励或记录",
        r"""s=data();before=copy.deepcopy(s);rejects(core.apply_batch,s,env(s,[submit(),review(),review('Z')]));same(s,before)""",
    ),
    "conflict": (
        "重复审核和旧版本禁止重复发奖",
        r"""s=approved();rejects(core.apply_batch,s,env(s,[review(version=1)]));rejects(core.apply_batch,s,env(s,[review(version=0,kind='revise')]));rejects(core.apply_batch,s,dict(env(s,[]),expected_revision=True))""",
    ),
    "replay": (
        "已提交请求回放忽略坏载荷",
        r"""s=approved();n=core.apply_batch(s,{'request_id':'b0','actions':None,'expected_revision':False,'extra':1});same(n,s);n['wallets']['U']=99;same(s['wallets']['U'],24)""",
    ),
    "preview": (
        "预览奖励与实际提交一致",
        r"""s=data();p=env(s,[submit(),review()]);before=copy.deepcopy((s,p));same(core.preview(s,p),{'base_revision':0,'next_revision':1,'wallet_delta':{'U':24},'replayed':False});same((s,p),before)""",
    ),
    "revise": (
        "人工改判撤回奖励并转给重复票",
        r"""s=run(data(),[submit('A','U',1),review('A'),submit('B','V',2),review('B')]);n=run(s,[review('A',1,'rejected','revise')]);same(n['wallets'],{'U':0,'V':24});same([e['delta'] for e in n['ledger']],[24,-24,24]);same(core.reconcile(n)['total_reward'],24)""",
    ),
    "restore": (
        "再次改判恢复最早票奖励且账目守恒",
        r"""s=run(data(),[submit('A','U',1),review('A'),submit('B','V',2),review('B')]);s=run(s,[review('A',1,'rejected','revise')]);s=run(s,[review('A',2,'approved','revise')]);same(core.reconcile(s)['wallets'],{'U':24,'V':0});same(len(s['ledger']),5);same(core.preview(s,{'request_id':'b0'})['replayed'],True)""",
    ),
    "tamper": (
        "奖励投影和账目不一致拒绝核对",
        r"""s=approved();s['wallets']['U']=999;rejects(core.reconcile,s);s=approved();s['ledger'][0]['delta']=1;rejects(core.reconcile,s);s=approved();s['receipts']['A']['reward']=0;rejects(core.reconcile,s)""",
    ),
    "large": (
        "大整数金额不能丢精度",
        r"""s=run(data(),[submit(document=doc(9007199254740993)),review()]);same(s['wallets']['U'],180143985094819)""",
    ),
    "cli": (
        "审核奖励CLI及核对结果保存",
        r"""s=cli('apply',{'state':data(),'envelope':env(data(),[submit(),review()])},True);same(cli('reconcile',s)['total_reward'],24);same(cli('summary',[{'id':'A','value':1}]),{'count':1,'total':1})""",
    ),
}

CONFIG = {
    "B1-v1": (
        "badcase",
        BAD_COMMON,
        BAD,
        ["normalize", "route", "profile", "source", "missing", "timing", "request", "cli"],
        ["image", "image_expiry"],
        ["normalize", "route"],
    ),
    "B3-v1": (
        "experiments",
        EXP_COMMON,
        EXP,
        ["normalize", "aggregate", "weighted", "funnel", "excluded", "boundary", "cli"],
        ["user", "tie", "compare"],
        ["aggregate", "weighted"],
    ),
    "B6-v1": (
        "retirement",
        RET_COMMON,
        RET,
        ["plan", "blocker", "chain", "phase", "atomic", "preview", "cli"],
        ["rollback", "rollback_atomic"],
        ["plan", "chain"],
    ),
    "B11-v1": (
        "receipts",
        REC_COMMON,
        REC,
        ["document", "fingerprint", "reward", "duplicate", "correction", "atomic", "preview", "cli"],
        ["revise", "restore", "tamper"],
        ["reward", "duplicate"],
    ),
}


def source(task_id, keys):
    package, common, cases, _, _, _ = CONFIG[task_id]
    result = COMMON.replace("PACKAGE", package) + common
    for key in keys:
        body = cases[key][1] if key in cases else key + "()"
        result += "\ndef check_" + key + "():\n    " + body + "\n"
    return result


def public(task_id, keys, probe=None):
    text = source(task_id, list(dict.fromkeys(keys + (probe or []))))
    if probe:
        text += "\nselected=" + repr(probe) + " if '--probe' in sys.argv else " + repr(keys) + "\n"
    else:
        text += "\nselected=" + repr(keys) + "\n"
    return text + "for key in selected:globals()['check_'+key]()\nprint('public checks passed')\n"


def grade(task_id):
    _, _, cases, _, _, _ = CONFIG[task_id]
    keys = list(cases) + ["legacy", "cli_invalid", "atomic_save", "docs_tests"]
    text = source(task_id, keys) + "\nchecks=[]\n"
    text += """for key,label in LABELS:
    try:globals()['check_'+key]();checks.append(dict(id=key,label=label,passed=True))
    except Exception as error:checks.append(dict(id=key,label=label,passed=False,error=type(error).__name__+':'+str(error)))
passed=all(c['passed'] for c in checks)
print(json.dumps(dict(passed=passed,checks=checks),ensure_ascii=False))
raise SystemExit(0 if passed else 1)
""".replace(
        "LABELS",
        repr(
            [
                (
                    k,
                    cases[k][0]
                    if k in cases
                    else {
                        "legacy": "旧接口兼容",
                        "cli_invalid": "文件输入和失败输出保护",
                        "atomic_save": "原子保存失败保留原文件",
                        "docs_tests": "项目测试可运行和README说明",
                    }[k],
                )
                for k in keys
            ]
        ),
    )
    return text
