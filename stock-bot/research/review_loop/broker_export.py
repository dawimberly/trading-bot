"""Read-only, pinned-account research export. No production imports or broker writes."""
from __future__ import annotations
import argparse, hashlib, json, math, os, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from vendor_post_sale_review import post_sale_report
BOOKS={'alpaca_live':False,'alpaca_paper_v2':True,'alpaca_paper':True}
NY=ZoneInfo('America/New_York')
TYPES='cash_dividend,stock_dividend,forward_split,reverse_split,unit_split,cash_merger,stock_merger,stock_and_cash_merger,spin_off,redemption,name_change,worthless_removal'

class ExportError(RuntimeError):pass

def atomic(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(obj,indent=2,allow_nan=False),encoding='utf8');os.replace(tmp,path)

def parse_env(path):
    result={}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:continue
        k,v=line.split('=',1);result[k.strip()]=v.strip().strip('\"').strip("'")
    return result

def expected_base(book,env):
    if book not in BOOKS:raise ExportError('unknown_book')
    if env.get('PAPER_TRADING','').lower().strip() not in ('true','false','yes','no','1','0'):raise ExportError('paper_flag_missing_or_ambiguous')
    paper=env['PAPER_TRADING'].lower().strip() in ('true','yes','1')
    if paper!=BOOKS[book]:raise ExportError('paper_flag_mismatch')
    return 'https://paper-api.alpaca.markets' if paper else 'https://api.alpaca.markets'

def fingerprint(account):
    if not isinstance(account,dict) or not account.get('id'):raise ExportError('account_id_missing')
    return hashlib.sha256(str(account['id']).encode()).hexdigest()

def check_account(account,pin):
    if fingerprint(account)!=pin:raise ExportError('account_identity_mismatch')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):raise ExportError('redirect_refused')

class ReadClient:
    def __init__(self,base,env,raw_dir=None,opener=None,budget_seconds=1200):
        self.base=base;self.raw_dir=Path(raw_dir) if raw_dir else None
        key=env.get('APCA_API_KEY_ID') or env.get('PAPER_APCA_API_KEY_ID')
        secret=env.get('APCA_API_SECRET_KEY') or env.get('PAPER_APCA_API_SECRET_KEY')
        if not key or not secret:raise ExportError('credentials_missing')
        self._headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret}
        self.opener=opener or urllib.request.build_opener(NoRedirect());self.calls=[];self.verified=False;self.started=time.monotonic();self.budget_seconds=budget_seconds
    def validate(self,url):
        u=urllib.parse.urlsplit(url)
        if u.scheme!='https' or u.username or u.password or u.fragment or u.port not in (None,443):raise ExportError('request_url_refused')
        host=urllib.parse.urlsplit(self.base).hostname
        trading={'/v2/account','/v2/positions','/v2/orders','/v2/account/activities','/v2/account/portfolio/history','/v2/calendar'}
        data={'/v2/stocks/bars','/v1/corporate-actions'}
        if not ((u.hostname==host and u.path in trading) or (u.hostname=='data.alpaca.markets' and u.path in data)):raise ExportError('request_route_refused')
        if not self.verified and (u.hostname!=host or u.path!='/v2/account'):raise ExportError('identity_check_required_first')
    def get(self,path,params=None,data=False):
        url=('https://data.alpaca.markets' if data else self.base)+path
        if params:url+='?'+urllib.parse.urlencode(params)
        self.validate(url)
        for attempt in range(3):
            if time.monotonic()-self.started>self.budget_seconds:raise ExportError('collection_time_budget')
            req=urllib.request.Request(url,headers=self._headers,method='GET')
            try:
                with self.opener.open(req,timeout=20) as response:
                    status=response.status;body=response.read()
                if status!=200:raise ExportError('http_'+str(status))
                payload=json.loads(body.decode('utf8'))
                note={'method':'GET','path':path,'params':params or {},'status':200,'received_at':datetime.now(timezone.utc).isoformat(),'body_sha256':hashlib.sha256(body).hexdigest()}
                self.calls.append(note)
                if self.raw_dir:atomic(self.raw_dir/(str(len(self.calls)).zfill(4)+'.json'),{'request':note,'response':payload})
                time.sleep(.1);return payload
            except urllib.error.HTTPError as e:
                if e.code in (429,500,502,503,504) and attempt<2:time.sleep(2**attempt);continue
                raise ExportError('http_'+str(e.code)) from None
            except ExportError:raise
            except (TimeoutError,OSError):
                if attempt<2:time.sleep(2**attempt);continue
                raise ExportError('network_timeout_or_error') from None
            except (ValueError,UnicodeError):raise ExportError('invalid_json_response') from None
        raise ExportError('retry_exhausted')

def activities(client,as_of,max_pages):
    rows=[];seen={};token=None;tokens=set();complete=False
    for page in range(max_pages):
        q={'page_size':100,'direction':'desc','until':as_of}
        if token:q['page_token']=token
        items=client.get('/v2/account/activities',q)
        if not isinstance(items,list):raise ExportError('activities_not_list')
        for item in items:
            if not isinstance(item,dict) or not item.get('id'):raise ExportError('activity_id_missing')
            ident=str(item['id']);signature=json.dumps(item,sort_keys=True)
            if ident in seen:
                if seen[ident]!=signature:raise ExportError('activity_conflicting_duplicate')
            else:seen[ident]=signature;rows.append(item)
        if len(items)<100:complete=True;break
        token=str(items[-1]['id'])
        if token in tokens:raise ExportError('activity_pagination_stuck')
        tokens.add(token)
    return rows,{'pages':page+1,'complete':complete,'truncated':not complete}

def object_pages(client,path,params,max_pages=50):
    pages=[];tokens=set();q=dict(params)
    for _ in range(max_pages):
        payload=client.get(path,q,data=True)
        if not isinstance(payload,dict):raise ExportError('page_not_object')
        pages.append(payload);token=payload.get('next_page_token')
        if not token:return pages,True
        if token in tokens:raise ExportError('object_pagination_stuck')
        tokens.add(token);q['page_token']=token
    return pages,False

def calendar_rows(rows):
    if not isinstance(rows,list):raise ExportError('calendar_not_list')
    out=[]
    for r in rows:
        day=str(r['date']);hh,mm=map(int,str(r['close']).split(':'))
        t=datetime.fromisoformat(day).replace(hour=hh,minute=mm,tzinfo=NY)
        out.append({'date':day,'close':t.isoformat()})
    if [x['date'] for x in out]!=sorted({x['date'] for x in out}):raise ExportError('calendar_not_unique_sorted')
    return out

def scope_actions(pages,complete,symbol,start,end):
    changing=[];unknown=[];dividends=[];verified=complete
    for page in pages:
        body=page.get('corporate_actions')
        if not isinstance(body,dict):verified=False;continue
        for kind,items in body.items():
            if not isinstance(items,list):verified=False;continue
            for row in items:
                if not isinstance(row,dict):verified=False;continue
                names={str(row[k]) for k in ('symbol','old_symbol','new_symbol') if row.get(k)}
                if not names:verified=False;continue
                if symbol not in names:continue
                day=next((str(row[k])[:10] for k in ('ex_date','effective_date','process_date') if row.get(k)),None)
                if not day:verified=False;continue
                try:datetime.fromisoformat(day)
                except ValueError:verified=False;continue
                if kind in ('cash_dividend','cash_dividends'):dividends.append(day)
                else:changing.extend(str(row[k])[:10] for k in ('ex_date','effective_date','process_date','payable_date') if row.get(k))
    result={'corporate_action_evidence':{'source':'alpaca_GET_corporate_actions','pages_complete':complete,'scoping_verified':verified}}
    if verified:result.update(corporate_actions_checked_from=start,corporate_actions_checked_through=end,position_changing_action_dates=sorted(set(changing)),uncertain_action_dates=unknown,dividend_dates_not_in_price=sorted(set(dividends)))
    return result

def load_plans(folder,book):
    rows=[];bad=0
    for path in sorted(Path(folder).glob('*.jsonl')):
        for line in path.read_text(encoding='utf8').splitlines():
            if not line.strip():continue
            try:r=json.loads(line)
            except ValueError:bad+=1;continue
            if isinstance(r,dict) and r.get('book')==book:rows.append(r)
    return rows,bad

def collect_book(book,client,pin,private,source_root,as_of,max_pages=200,symbol_cap=80):
    account=client.get('/v2/account');check_account(account,pin);client.verified=True
    account_at=datetime.now(timezone.utc).isoformat()
    atomic(private/'account.json',{'observed_at':account_at,'response':account})
    snapshots={};errors={}
    for name,path,q in [('positions','/v2/positions',{}),('open_orders','/v2/orders',{'status':'open','limit':500}),('portfolio_history','/v2/account/portfolio/history',{'period':'1A','timeframe':'1D','extended_hours':'false'})]:
        try:snapshots[name]=client.get(path,q)
        except ExportError as e:errors[name]=str(e)
    rows,walk=activities(client,as_of.isoformat(),max_pages)
    atomic(private/'activities.json',{'as_of':as_of.isoformat(),'coverage':walk,'rows':rows})
    fills=[r for r in rows if r.get('activity_type')=='FILL' and r.get('side') in ('buy','sell') and r.get('asset_class','us_equity')=='us_equity' and '/' not in str(r.get('symbol',''))]
    if not fills:raise ExportError('no_equity_fills')
    start=min(str(r['transaction_time'])[:10] for r in fills)
    cal=calendar_rows(client.get('/v2/calendar',{'start':(datetime.fromisoformat(start)-timedelta(days=7)).date().isoformat(),'end':(as_of+timedelta(days=90)).date().isoformat()}))
    closed=[r for r in cal if datetime.fromisoformat(r['close'])<=as_of]
    if not closed:raise ExportError('no_completed_exchange_session')
    end=closed[-1]['date'];endtime=closed[-1]['close']
    sold=list(dict.fromkeys(str(r['symbol']) for r in sorted(fills,key=lambda x:x['transaction_time'],reverse=True) if r.get('side')=='sell'))
    selected=sold[:symbol_cap];prices={};price_errors={};action_unknown=set()
    for offset in range(0,len(selected),20):
        batch=selected[offset:offset+20]
        try:
            ap,ac=object_pages(client,'/v1/corporate-actions',{'symbols':','.join(batch),'start':start,'end':end,'types':TYPES,'limit':1000})
        except ExportError:ap=[];ac=False
        try:
            bp,bc=object_pages(client,'/v2/stocks/bars',{'symbols':','.join(batch),'start':start+'T00:00:00Z','end':endtime,'timeframe':'1Day','adjustment':'raw','feed':'sip','limit':10000})
            allbars={s:[] for s in batch}
            for p in bp:
                if not isinstance(p.get('bars'),dict):raise ExportError('bars_shape_invalid')
                for s in batch:allbars[s].extend(p['bars'].get(s,[]))
            for s in batch:
                normalized=[];days=set()
                for r in allbars[s]:
                    dt=datetime.fromisoformat(r['t'].replace('Z','+00:00'))
                    if dt.tzinfo is None:raise ExportError('naive_bar_timestamp')
                    utc=dt.astimezone(timezone.utc);day=utc.date().isoformat() if (utc.hour,utc.minute)==(0,0) else dt.astimezone(NY).date().isoformat()
                    if day in days:raise ExportError('duplicate_daily_bar')
                    days.add(day);c,h,l=map(float,(r['c'],r['h'],r['l']))
                    if not all(math.isfinite(n) and n>0 for n in (c,h,l)) or not l<=c<=h:raise ExportError('invalid_bar_ohlc')
                    if day<=end:normalized.append({'date':day,'close':c,'high':h,'low':l})
                action=scope_actions(ap,ac,s,start,end)
                if not action['corporate_action_evidence']['scoping_verified']:action_unknown.add(s)
                prices[s]={'basis':'raw_same_units_as_broker' if bc else 'unverified','unit_alignment_verified':bc,'unit_evidence':{'source':'alpaca_GET_bars','feed':'sip','adjustment_requested':'raw','pages_complete':bc},'bars':normalized,**action}
        except (ExportError,KeyError,TypeError,ValueError) as e:
            code=str(e) if isinstance(e,ExportError) else 'invalid_price_response'
            for s in batch:price_errors[s]=code
    plans,badplans=load_plans(source_root/'data'/'trade_plans'/book,book)
    report=post_sale_report(book=book,fills=fills,calendar=cal,prices=prices,as_of=as_of.isoformat(),plans=plans,initial_inventory=None)
    report['account_identity']='verified_against_preexisting_local_report_pin'
    report['coverage']={'complete_from_start':walk['complete'],'truncated':walk['truncated'],'pages':walk['pages'],'fills_stored':len(fills),'fills_added':None,'pagination_error':None,'conflicts':[],
        'initial_inventory':'unknown','initial_inventory_detail':'corporate_action_and_initial_lot_reconciliation_not_implemented',
        'sale_symbols_priced':list(prices),'sale_symbols_deferred':sold[symbol_cap:],'price_errors':price_errors,'positions_error':errors.get('positions'),'calendar_error':None,
        'plans_skipped':badplans,'plans_loaded':len(plans),'feed':'sip','adjustment':'raw','action_scoping_unverified_symbols':sorted(action_unknown),
        'account_pin_verified':True,'collection_window_not_atomic':True,'portfolio_accounting_reconciled':False}
    report['generated_at']=datetime.now(timezone.utc).isoformat()
    report['account_snapshots_are_not_trade_time_snapshots']=True
    report['open_positions']=[]
    atomic(private/'report.json',report);atomic(private/'snapshots.json',snapshots);atomic(private/'calendar.json',cal);atomic(private/'prices.json',prices);atomic(private/'requests.json',client.calls)
    check_account(client.get('/v2/account'),pin)
    # A failed post-check never publishes a prepared report.
    return report,{'book':book,'account_pin_verified':True,'complete_activity_pagination':walk['complete'],'activity_pages':walk['pages'],'fill_count':len(fills),'observed_activity_types':sorted({str(r.get('activity_type','')) for r in rows}),'open_order_count':len(snapshots['open_orders']) if isinstance(snapshots.get('open_orders'),list) else None,'collected_at':report['generated_at'],'report_as_of':report['as_of'],'errors':sorted(errors),'private_evidence_only':True}

def bootstrap(source,private):
    path=private/'pins.json'
    if path.exists():raise ExportError('pins_already_exist')
    pins={}
    for book in BOOKS:
        r=json.loads((source/'reports'/'trade_review'/(book+'.json')).read_text(encoding='utf8'))
        value=r.get('coverage',{}).get('account_sha256')
        if r.get('book')!=book or not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):raise ExportError('prior_account_pin_unavailable')
        pins[book]={'account_sha256':value,'source_report_as_of':r['as_of'],'source':'preexisting_local_trade_review_not_independent_reauthentication'}
    if len({p['account_sha256'] for p in pins.values()})!=len(BOOKS):raise ExportError('duplicate_account_pin')
    atomic(path,pins)

def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--source-root',type=Path,required=True);a.add_argument('--private-root',type=Path,default=Path(__file__).parent/'private');a.add_argument('--bootstrap-pins',action='store_true');a.add_argument('--fetch',action='store_true');a.add_argument('--book',action='append',choices=BOOKS);a.add_argument('--max-pages',type=int,default=200);a.add_argument('--symbol-cap',type=int,default=80);args=a.parse_args()
    source=args.source_root.resolve();private=args.private_root.resolve()
    if private==source or source in private.parents:raise ExportError('output_must_be_outside_production_source')
    if not (1<=args.max_pages<=500 and 1<=args.symbol_cap<=500):raise ExportError('collection_budget_invalid')
    if args.bootstrap_pins:bootstrap(source,private)
    if not args.fetch:
        print(json.dumps({'status':'PINS_READY' if (private/'pins.json').exists() else 'PINS_REQUIRED','broker_requests':0}));return 0
    pins=json.loads((private/'pins.json').read_text());run=datetime.now(timezone.utc);runid=run.strftime('%Y%m%dT%H%M%S%fZ');out=private/'runs'/runid;receipts=[]
    # Only writes inside private research output; .env/source/production logs remain read-only.
    def guard(event,values):
        if event=='open':
            p,mode,flags=values
            if isinstance(p,(str,bytes,os.PathLike)) and ((isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))):
                dest=Path(p).resolve()
                if dest!=private and private not in dest.parents:raise ExportError('write_outside_private_root')
    import sys
    sys.addaudithook(guard)
    for book in args.book or BOOKS:
        try:
            env_path=source/'data'/'portal'/'users'/'dawimberly'/'books'/book/'.env';env=parse_env(env_path);base=expected_base(book,env)
            client=ReadClient(base,env,out/book/'raw');r,receipt=collect_book(book,client,pins[book]['account_sha256'],out/book,source,run,args.max_pages,args.symbol_cap)
            atomic(private/'reports'/(book+'.json'),r);receipts.append(receipt)
            print(json.dumps(receipt),flush=True)
        except Exception as exc:
            code=str(exc) if isinstance(exc,ExportError) else type(exc).__name__
            atomic(out/book/'failure.json',{'book':book,'error':code,'at':datetime.now(timezone.utc).isoformat()})
            receipts.append({'book':book,'error':code});print(json.dumps({'book':book,'status':'BLOCKED','error':code}),flush=True)
    atomic(out/'receipt.json',receipts)
    return int(any('error' in r for r in receipts))
if __name__=='__main__':raise SystemExit(main())
