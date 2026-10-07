"""Fresh local simulations; never import company helpers or previous model answers."""

UTIL = r'''from copy import deepcopy
import hashlib,json,os,tempfile
from pathlib import Path

def ident(x):
    if not isinstance(x,str) or not x or x.strip()!=x:raise ValueError('identifier')
    return x
def integer(x,minimum=0):
    if type(x) is not int or x<minimum:raise ValueError('integer')
    return x
def fields(x,required,optional=()):
    if not isinstance(x,dict) or not set(required)<=set(x) or set(x)-set(required)-set(optional):raise ValueError('fields')
def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()
def read(path):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError,UnicodeError) as e:raise ValueError('JSON') from e
def save(x,path):
    path=Path(path);name=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as f:
            name=f.name;f.write(canonical(x));f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if name and os.path.exists(name):os.unlink(name)
def legacy_summary(rows):
    if not isinstance(rows,list):raise ValueError('rows')
    for r in rows:
        fields(r,('id','value'));ident(r['id']);integer(r['value'])
    return {'count':len(rows),'total':sum(r['value'] for r in rows)}
'''

BADCASE = r'''from .util import *

RULES={'dislike':('category',180),'bought':('category',30),'privacy':('category',365),'image':('spu',365)}
def normalize(raw):
    fields(raw,('schema','requests','feedback','products'))
    if type(raw['schema']) is not int or raw['schema'] not in (1,2):raise ValueError('schema')
    if not all(isinstance(raw[k],list) for k in ('requests','feedback','products')):raise ValueError('arrays')
    out={'schema':2,'requests':[],'feedback':[],'products':{}};ids=set()
    for p in raw['products']:
        fields(p,('sku','category','spu'));ident(p['sku'])
        for k in ('category','spu'):
            if p[k] is not None:ident(p[k])
        if p['sku'] in out['products']:raise ValueError('duplicate product')
        out['products'][p['sku']]=deepcopy(p)
    for f in raw['feedback']:
        fields(f,('id','user','sku','at','kind'));ident(f['id']);ident(f['user']);ident(f['sku']);integer(f['at'])
        if not isinstance(f['kind'],str) or f['kind'] not in RULES or f['sku'] not in out['products'] or f['id'] in ids:raise ValueError('feedback')
        ids.add(f['id']);out['feedback'].append(deepcopy(f))
    ids=set()
    for r in raw['requests']:
        if raw['schema']==1:
            fields(r,('id','user','at','placement','items','profile_ok','joined','trace'))
            n=deepcopy(r)
        else:
            fields(r,('request_id','user_id','timestamp','slot','results','signals'))
            fields(r['signals'],('profile_ok','joined','trace'))
            n=dict(id=r['request_id'],user=r['user_id'],at=r['timestamp'],placement=r['slot'],items=deepcopy(r['results']),**deepcopy(r['signals']))
        for k in ('id','user','placement'):ident(n[k])
        integer(n['at'])
        if n['id'] in ids:raise ValueError('duplicate request')
        ids.add(n['id'])
        if n['profile_ok'] not in (True,False,None) or n['profile_ok'] is not None and type(n['profile_ok']) is not bool:raise ValueError('profile')
        if type(n['joined']) is not bool or type(n['trace']) is not bool or not isinstance(n['items'],list):raise ValueError('signals')
        seen=set()
        for item in n['items']:
            fields(item,('sku','source','hit'));ident(item['sku'])
            if item['sku'] not in out['products'] or item['sku'] in seen or item['source'] not in ('organic','ad'):raise ValueError('item')
            if item['hit'] is not None and type(item['hit']) is not bool:raise ValueError('hit')
            seen.add(item['sku'])
        out['requests'].append(n)
    out['requests'].sort(key=lambda r:(r['at'],r['id']));out['feedback'].sort(key=lambda f:(f['at'],f['id']))
    return out

def diagnose(raw,query):
    d=normalize(raw);fields(query,('user','sku','at','placement'))
    for k in ('user','sku','placement'):ident(query[k])
    integer(query['at']);p=d['products'].get(query['sku'])
    if p is None:raise ValueError('unknown product')
    requests=[r for r in d['requests'] if all(r[k]==query[k] for k in ('user','at','placement'))]
    if len(requests)>1:raise ValueError('ambiguous request')
    if not requests:return dict(reason='no_request',request_id=None,feedback_ids=[],evidence=[],confirmed=False)
    r=requests[0];items=[i for i in r['items'] if i['sku']==query['sku']]
    fs=[];missing=False
    for f in d['feedback']:
        if f['user']!=query['user'] or f['at']>r['at']:continue
        field,days=RULES[f['kind']]
        if r['at']-f['at']>=days*86400:continue
        fp=d['products'][f['sku']]
        if p[field] is None or fp[field] is None:missing=True;continue
        if p[field]==fp[field]:fs.append(f)
    evidence=[dict(source='requests',id=r['id'])]+[dict(source='feedback',id=f['id']) for f in fs]
    if not items:reason='not_returned';confirmed=True
    elif items[0]['source']=='ad':reason='ad_route';confirmed=True
    elif not r['trace']:reason='insufficient_trace';confirmed=False
    elif items[0]['hit'] is True:reason='inconsistent_trace';confirmed=False
    elif not fs:reason='missing_product_relation' if missing else 'no_active_feedback';confirmed=not missing
    elif r['profile_ok'] is None:reason='unknown_profile';confirmed=False
    elif r['profile_ok'] is False:reason='profile_unavailable';confirmed=True
    elif not r['joined']:reason='route_not_integrated';confirmed=True
    else:reason='rule_violation';confirmed=True
    return dict(reason=reason,request_id=r['id'],feedback_ids=[f['id'] for f in fs],evidence=evidence,confirmed=confirmed)

def batch(raw,queries):
    if not isinstance(queries,list):raise ValueError('queries')
    return [diagnose(raw,q) for q in queries]
def compare(before,after,queries):
    a=batch(before,queries);b=batch(after,queries)
    return [dict(index=i,before=x['reason'],after=y['reason'],changed=x!=y) for i,(x,y) in enumerate(zip(a,b))]
def dispatch(command,p):
    if command=='summary':return legacy_summary(p)
    if command=='normalize':return normalize(p)
    if command=='diagnose':return diagnose(p['data'],p['query'])
    if command=='batch':return batch(p['data'],p['queries'])
    if command=='compare':return compare(p['before'],p['after'],p['queries'])
    raise ValueError('command')
'''

EXPERIMENT = r'''from .util import *
from fractions import Fraction

def normalize(raw):
    fields(raw,('schema','assignments','events'))
    if type(raw['schema']) is not int or raw['schema'] not in (1,2):raise ValueError('schema')
    if not isinstance(raw['assignments'],list) or not isinstance(raw['events'],list):raise ValueError('arrays')
    a={};events={}
    for x in raw['assignments']:
        fields(x,('user','group','cohort','version','start','end'));ident(x['user']);ident(x['cohort']);ident(x['version'])
        integer(x['start']);integer(x['end'])
        if x['group'] not in ('control','treatment') or x['end']<=x['start']:raise ValueError('assignment')
        a.setdefault(x['user'],[]).append(deepcopy(x))
    for rows in a.values():
        rows.sort(key=lambda x:x['start'])
        if any(x['end']>y['start'] for x,y in zip(rows,rows[1:])):raise ValueError('overlapping assignment')
    for x in raw['events']:
        if raw['schema']==1:
            fields(x,('id','user','at','version','returned','clicked','converted'));n=deepcopy(x)
        else:
            fields(x,('event_id','user_id','timestamp','strategy','stages'));fields(x['stages'],('returned','clicked','converted'))
            n=dict(id=x['event_id'],user=x['user_id'],at=x['timestamp'],version=x['strategy'],**deepcopy(x['stages']))
        for k in ('id','user','version'):ident(n[k])
        integer(n['at'])
        for k in ('returned','clicked','converted'):
            if type(n[k]) is not bool:raise ValueError('stage boolean')
        if n['converted'] and not n['clicked'] or n['clicked'] and not n['returned']:raise ValueError('funnel order')
        if n['id'] in events and events[n['id']]!=n:raise ValueError('conflicting duplicate')
        events[n['id']]=n
    return {'schema':2,'assignments':[x for u in sorted(a) for x in a[u]],'events':sorted(events.values(),key=lambda x:(x['at'],x['id']))}

def fraction(n,d):
    if not d:return None
    x=Fraction(n,d);return [x.numerator,x.denominator]
def metric(rows):
    n=len(rows);returned=sum(r['returned'] for r in rows);clicked=sum(r['clicked'] for r in rows);converted=sum(r['converted'] for r in rows)
    return dict(requests=n,returned=returned,clicked=clicked,converted=converted,return_rate=fraction(returned,n),ctr=fraction(clicked,returned),cvr=fraction(converted,clicked),conversion_rate=fraction(converted,n))
def analyze(raw,query):
    d=normalize(raw);fields(query,('start','end','version'),('unit','weights'))
    integer(query['start']);integer(query['end']);ident(query['version'])
    if query['end']<=query['start'] or query.get('unit','request') not in ('request','user'):raise ValueError('query')
    matched=[];excluded={'unassigned':0,'version':0};assign={}
    for a in d['assignments']:assign.setdefault(a['user'],[]).append(a)
    for e in d['events']:
        if not query['start']<=e['at']<query['end']:continue
        found=[a for a in assign.get(e['user'],[]) if a['start']<=e['at']<a['end']]
        if not found:excluded['unassigned']+=1;continue
        a=found[0]
        if a['version']!=query['version'] or e['version']!=a['version']:excluded['version']+=1;continue
        matched.append(dict(e,group=a['group'],cohort=a['cohort']))
    if query.get('unit','request')=='user':
        latest={}
        for e in matched:latest[e['user']]=e
        matched=list(latest.values())
    cohorts=sorted({e['cohort'] for e in matched});groups={};by={}
    for g in ('control','treatment'):
        groups[g]=metric([e for e in matched if e['group']==g])
        by[g]={c:metric([e for e in matched if e['group']==g and e['cohort']==c]) for c in cohorts}
    harmed=[]
    for c in cohorts:
        x=by['control'][c]['conversion_rate'];y=by['treatment'][c]['conversion_rate']
        if x and y and Fraction(*y)<Fraction(*x):harmed.append(c)
    weights=query.get('weights');standardized=None
    if weights is not None:
        if not isinstance(weights,dict) or not weights:raise ValueError('weights')
        for c,v in weights.items():ident(c);integer(v,1)
        standardized={}
        for g in ('control','treatment'):
            if any(c not in by[g] or by[g][c]['conversion_rate'] is None for c in weights):raise ValueError('missing weighted cohort')
            value=sum((Fraction(*by[g][c]['conversion_rate'])*v for c,v in weights.items()),Fraction())/sum(weights.values())
            standardized[g]=[value.numerator,value.denominator]
    return dict(unit=query.get('unit','request'),groups=groups,cohorts=by,harmed_cohorts=harmed,excluded=excluded,evidence_ids=sorted(e['id'] for e in matched),standardized=standardized)
def compare(raw,before,after):return {'before':analyze(raw,before),'after':analyze(raw,after)}
def dispatch(command,p):
    if command=='summary':return legacy_summary(p)
    if command=='normalize':return normalize(p)
    if command=='analyze':return analyze(p['data'],p['query'])
    if command=='compare':return compare(p['data'],p['before'],p['after'])
    raise ValueError('command')
'''

RETIREMENT = r'''from .util import *

def validate(raw):
    fields(raw,('revision','features','consumers','data','backup','batches','events'))
    integer(raw['revision'])
    for k in ('features','consumers','data','backup'):
        if not isinstance(raw[k],dict):raise ValueError(k)
    for key,f in raw['features'].items():
        ident(key);fields(f,('depends','status','verified'))
        if f['status'] not in ('active','retiring','retired') or type(f['verified']) is not bool or not isinstance(f['depends'],list):raise ValueError('feature')
        for d in f['depends']:ident(d)
        if len(set(f['depends']))!=len(f['depends']) or any(d not in raw['features'] or d==key for d in f['depends']):raise ValueError('dependency')
        if key not in raw['data'] or not isinstance(raw['data'][key],list):raise ValueError('data')
    for key,refs in raw['consumers'].items():
        ident(key)
        if not isinstance(refs,list):raise ValueError('consumer')
        for x in refs:ident(x)
        if len(set(refs))!=len(refs) or any(x not in raw['features'] for x in refs):raise ValueError('consumer')
    for key,rows in raw['backup'].items():
        ident(key)
        if key not in raw['features'] or not isinstance(rows,list):raise ValueError('backup')
    visiting=set();done=set()
    def dfs(k):
        if k in visiting:raise ValueError('cycle')
        if k in done:return
        visiting.add(k)
        for x in raw['features'][k]['depends']:dfs(x)
        visiting.remove(k);done.add(k)
    for k in raw['features']:dfs(k)
    if not isinstance(raw['batches'],list) or not isinstance(raw['events'],list) or len(raw['batches'])!=raw['revision'] or len(raw['events'])!=raw['revision'] or len(set(raw['batches']))!=len(raw['batches']):raise ValueError('log')
    for key in raw['batches']:ident(key)
    for n,e in enumerate(raw['events'],1):
        fields(e,('revision','request_id','actions'))
        integer(e['revision'],1);ident(e['request_id'])
        if not isinstance(e['actions'],list):raise ValueError('event actions')
        if e['revision']!=n or e['request_id']!=raw['batches'][n-1]:raise ValueError('event')
    return deepcopy(raw)
def plan(state,targets):
    s=validate(state)
    if not isinstance(targets,list) or not targets:raise ValueError('targets')
    for k in targets:ident(k)
    if len(set(targets))!=len(targets) or any(k not in s['features'] for k in targets):raise ValueError('targets')
    chosen=set(targets);changed=True
    while changed:
        changed=False
        for k,f in s['features'].items():
            if k not in chosen and f['status']!='retired' and set(f['depends'])&chosen:chosen.add(k);changed=True
    order=[];remaining=set(chosen)
    while remaining:
        ready=sorted(k for k in remaining if not any(k in s['features'][j]['depends'] for j in remaining))
        if not ready:raise ValueError('cycle')
        for k in ready:order.append(k);remaining.remove(k)
    blockers=sorted(c for c,refs in s['consumers'].items() if set(refs)&chosen)
    return {'order':order,'blockers':blockers,'safe':not blockers}
def apply_batch(state,envelope):
    s=validate(state)
    if not isinstance(envelope,dict):raise ValueError('envelope')
    key=ident(envelope.get('request_id'))
    if key in s['batches']:return s
    fields(envelope,('request_id','expected_revision','actions'));integer(envelope['expected_revision'])
    if envelope['expected_revision']!=s['revision'] or not isinstance(envelope['actions'],list):raise ValueError('revision/actions')
    for a in envelope['actions']:
        if not isinstance(a,dict):raise ValueError('action')
        kind=a.get('kind')
        if kind=='detach':
            fields(a,('kind','consumer'));ident(a['consumer'])
            if a['consumer'] not in s['consumers']:raise ValueError('consumer')
            del s['consumers'][a['consumer']];continue
        if kind=='attach':
            fields(a,('kind','consumer','features'));ident(a['consumer'])
            refs=a['features']
            if not isinstance(refs,list):raise ValueError('active references')
            for x in refs:ident(x)
            if len(set(refs))!=len(refs) or any(k not in s['features'] or s['features'][k]['status']!='active' for k in refs):raise ValueError('active references')
            s['consumers'][a['consumer']]=deepcopy(refs);continue
        fields(a,('kind','feature'));k=ident(a['feature'])
        if k not in s['features']:raise ValueError('feature')
        f=s['features'][k]
        if kind=='rollback':
            if f['status']!='retiring':raise ValueError('rollback status')
            if k in s['backup']:s['data'][k]=s['backup'].pop(k)
            f['status']='active';f['verified']=False;continue
        if any(k in refs for refs in s['consumers'].values()):raise ValueError('live consumer')
        if any(k in x['depends'] and x['status']!='retired' for x in s['features'].values()):raise ValueError('live dependent')
        if kind=='mark':
            if f['status']!='active':raise ValueError('mark status')
            f['status']='retiring';f['verified']=False
        elif kind=='clean':
            if f['status']!='retiring' or k in s['backup']:raise ValueError('clean status')
            s['backup'][k]=deepcopy(s['data'][k]);s['data'][k]=[];f['verified']=False
        elif kind=='verify':
            if f['status']!='retiring' or k not in s['backup'] or s['data'][k]:raise ValueError('verify status')
            f['verified']=True
        elif kind=='finish':
            if f['status']!='retiring' or not f['verified'] or s['data'][k]:raise ValueError('finish status')
            f['status']='retired'
        else:raise ValueError('kind')
    s['revision']+=1;s['batches'].append(key);s['events'].append(dict(revision=s['revision'],request_id=key,actions=deepcopy(envelope['actions'])))
    return validate(s)
def preview(state,envelope):
    n=apply_batch(state,envelope)
    return dict(base_revision=state['revision'],next_revision=n['revision'],changed=sorted(k for k in n['features'] if n['features'][k]!=state['features'][k] or n['data'][k]!=state['data'][k]),replayed=n['revision']==state['revision'])
def reconcile(state):
    s=validate(state)
    return {'active':sorted(k for k,f in s['features'].items() if f['status']=='active'),'retiring':sorted(k for k,f in s['features'].items() if f['status']=='retiring'),'retired':sorted(k for k,f in s['features'].items() if f['status']=='retired'),'live_rows':sum(len(x) for x in s['data'].values()),'backup_rows':sum(len(x) for x in s['backup'].values()),'revision':s['revision']}
def dispatch(command,p):
    if command=='summary':return legacy_summary(p)
    if command=='validate':return validate(p)
    if command=='plan':return plan(p['state'],p['targets'])
    if command=='apply':return apply_batch(p['state'],p['envelope'])
    if command=='preview':return preview(p['state'],p['envelope'])
    if command=='reconcile':return reconcile(p)
    raise ValueError('command')
'''

RECEIPTS = r'''from .util import *

def document(raw):
    fields(raw,('merchant','date','number','items'))
    for k in ('merchant','date','number'):
        if not isinstance(raw[k],str) or not raw[k].strip():raise ValueError('document text')
    if not isinstance(raw['items'],list) or not raw['items']:raise ValueError('items')
    items=[]
    for x in raw['items']:
        fields(x,('name','quantity','unit_price'));ident(x['name']);integer(x['quantity'],1);integer(x['unit_price'])
        items.append(deepcopy(x))
    n={k:raw[k].strip().casefold() for k in ('merchant','date','number')};n['items']=items
    return n
def fingerprint(raw):
    n=document(raw);return digest({k:n[k] for k in ('merchant','date','number')})
def validate(raw):
    fields(raw,('revision','receipts','wallets','ledger','batches','events'));integer(raw['revision'])
    if not isinstance(raw['receipts'],dict) or not isinstance(raw['wallets'],dict):raise ValueError('maps')
    for key,r in raw['receipts'].items():
        ident(key);fields(r,('id','user','at','document','status','version','reward'))
        if r['id']!=key or r['status'] not in ('pending','approved','rejected'):raise ValueError('receipt')
        ident(r['user']);integer(r['at']);integer(r['version']);integer(r['reward']);document(r['document'])
    for key,v in raw['wallets'].items():ident(key);integer(v)
    if not all(isinstance(raw[k],list) for k in ('ledger','batches','events')) or len(raw['batches'])!=raw['revision'] or len(raw['events'])!=raw['revision'] or len(set(raw['batches']))!=len(raw['batches']):raise ValueError('logs')
    for key in raw['batches']:ident(key)
    for i,e in enumerate(raw['events'],1):
        fields(e,('revision','request_id','actions'))
        integer(e['revision'],1);ident(e['request_id'])
        if not isinstance(e['actions'],list):raise ValueError('event actions')
        if e['revision']!=i or e['request_id']!=raw['batches'][i-1]:raise ValueError('event')
    return deepcopy(raw)
def expected_rewards(state):
    rewards={k:0 for k in state['receipts']};seen=set()
    for r in sorted(state['receipts'].values(),key=lambda r:(r['at'],r['id'])):
        if r['status']!='approved':continue
        key=fingerprint(r['document'])
        if key in seen:continue
        seen.add(key);amount=sum(x['quantity']*x['unit_price'] for x in r['document']['items'])
        rewards[r['id']]=amount//100
    return rewards
def apply_batch(state,envelope):
    s=validate(state)
    if not isinstance(envelope,dict):raise ValueError('envelope')
    key=ident(envelope.get('request_id'))
    if key in s['batches']:return s
    fields(envelope,('request_id','expected_revision','actions'));integer(envelope['expected_revision'])
    if envelope['expected_revision']!=s['revision'] or not isinstance(envelope['actions'],list):raise ValueError('revision/actions')
    for a in envelope['actions']:
        if not isinstance(a,dict):raise ValueError('action')
        kind=a.get('kind')
        if kind=='submit':
            fields(a,('kind','id','user','at','document'));ident(a['id']);ident(a['user']);integer(a['at']);d=document(a['document'])
            if a['id'] in s['receipts']:raise ValueError('duplicate id')
            s['receipts'][a['id']]=dict(id=a['id'],user=a['user'],at=a['at'],document=d,status='pending',version=0,reward=0)
        elif kind in ('review','revise','correct'):
            fields(a,('kind','id','expected_version','document') if kind=='correct' else ('kind','id','expected_version','status'))
            ident(a['id']);integer(a['expected_version']);r=s['receipts'].get(a['id'])
            if r is None or r['version']!=a['expected_version']:raise ValueError('receipt version')
            if kind=='correct':
                if r['status']!='pending':raise ValueError('correction status')
                r['document']=document(a['document'])
            else:
                if a['status'] not in ('approved','rejected') or (kind=='review' and r['status']!='pending') or (kind=='revise' and r['status']=='pending'):raise ValueError('review status')
                r['status']=a['status']
            r['version']+=1
        else:raise ValueError('kind')
    rewards=expected_rewards(s)
    for rid in sorted(s['receipts']):
        r=s['receipts'][rid];delta=rewards[rid]-r['reward'];r['reward']=rewards[rid]
        if delta:s['ledger'].append(dict(receipt_id=rid,user=r['user'],delta=delta,revision=s['revision']+1,request_id=key))
    wallets={r['user']:0 for r in s['receipts'].values()}
    for r in s['receipts'].values():wallets[r['user']]+=r['reward']
    s['wallets']=dict(sorted(wallets.items()));s['revision']+=1;s['batches'].append(key);s['events'].append(dict(revision=s['revision'],request_id=key,actions=deepcopy(envelope['actions'])))
    return validate(s)
def reconcile(state):
    s=validate(state);rewards=expected_rewards(s);wallets={r['user']:0 for r in s['receipts'].values()};ledger={}
    for r in s['receipts'].values():wallets[r['user']]+=rewards[r['id']]
    for e in s['ledger']:
        fields(e,('receipt_id','user','delta','revision','request_id'));ident(e['receipt_id']);ident(e['user'])
        if type(e['delta']) is not int:raise ValueError('ledger delta')
        integer(e['revision'],1);ident(e['request_id'])
        ledger[e['user']]=ledger.get(e['user'],0)+e['delta']
    if any(r['reward']!=rewards[r['id']] for r in s['receipts'].values()) or s['wallets']!=wallets or any(ledger.get(u,0)!=v for u,v in wallets.items()) or any(u not in wallets for u in ledger):raise ValueError('inconsistent rewards')
    return {'revision':s['revision'],'receipts':len(s['receipts']),'approved':sum(r['status']=='approved' for r in s['receipts'].values()),'rewarded':sum(v>0 for v in rewards.values()),'total_reward':sum(wallets.values()),'wallets':dict(sorted(wallets.items()))}
def preview(state,envelope):
    n=apply_batch(state,envelope)
    return dict(base_revision=state['revision'],next_revision=n['revision'],wallet_delta={u:n['wallets'].get(u,0)-state['wallets'].get(u,0) for u in sorted(set(n['wallets'])|set(state['wallets']))},replayed=n['revision']==state['revision'])
def dispatch(command,p):
    if command=='summary':return legacy_summary(p)
    if command=='document':return document(p)
    if command=='fingerprint':return fingerprint(p)
    if command=='validate':return validate(p)
    if command=='apply':return apply_batch(p['state'],p['envelope'])
    if command=='preview':return preview(p['state'],p['envelope'])
    if command=='reconcile':return reconcile(p)
    raise ValueError('command')
'''
