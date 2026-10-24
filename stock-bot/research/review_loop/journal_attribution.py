"""Conservative legacy-journal attribution; no order submission or invented trade basis."""
from __future__ import annotations
import hashlib,json
from collections import Counter,defaultdict
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation,ROUND_HALF_EVEN
from zoneinfo import ZoneInfo
ZONE=ZoneInfo('America/Chicago')
WINDOW_SECONDS=120
QTY_TOL=Decimal('0.00000001')

def when(value):
    dt=datetime.fromisoformat(str(value).strip().replace('Z','+00:00'))
    if dt.tzinfo is None:dt=dt.replace(tzinfo=ZONE)
    return dt.astimezone(timezone.utc)

def number(value):
    n=Decimal(str(value).strip())
    if not n.is_finite():raise ValueError('nonfinite_decimal')
    return n

def journal_records(sources):
    """sources is {source_name: already padded list-of-dict CSV records}."""
    groups={};source_stats={};bad=[];all_dates=[]
    for source,rows in sources.items():
        stats=Counter()
        for offset,raw in enumerate(rows,2):
            row={str(k):str(v).strip() for k,v in raw.items()};event=row.get('event','').lower();stats[event]+=1
            try:t=when(row.get('timestamp',''));all_dates.append(t)
            except (ValueError,TypeError):
                bad.append({'source':source,'csv_record':offset,'error':'invalid_or_missing_timestamp'});continue
            if event not in ('fill','signal','exit'):continue
            row['event']=event
            key=hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()
            slot=groups.setdefault(key,{'record_id':key,'row':row,'time':t.isoformat(),'provenance':[],'source_occurrences':Counter()})
            slot['provenance'].append({'source':source,'csv_record':offset,'physical_line_not_asserted':True});slot['source_occurrences'][source]+=1
        source_stats[source]={'rows':len(rows),'events':dict(stats)}
    records=[]
    for v in groups.values():
        v['ambiguous_same_source_repeat']=max(v.pop('source_occurrences').values())>1
        records.append(v)
    return records,{'sources':source_stats,'invalid_timestamp_rows':len(bad),'timestamp_errors':bad,'first_journal_timestamp_utc':min(all_dates).isoformat() if all_dates else None,'last_journal_timestamp_utc':max(all_dates).isoformat() if all_dates else None}

def fill_units(activities):
    seen={};fills=[];invalid=[];orders=defaultdict(list)
    for a in activities:
        if a.get('activity_type')!='FILL':continue
        ident=str(a.get('id') or '')
        if not ident:raise ValueError('execution_id_required')
        sig=json.dumps(a,sort_keys=True)
        if ident in seen:
            if seen[ident]!=sig:raise ValueError('conflicting_execution_duplicate')
            continue
        seen[ident]=sig
        try:
            raw_time=datetime.fromisoformat(str(a['transaction_time']).replace('Z','+00:00'))
            if raw_time.tzinfo is None:raise ValueError('broker_timestamp_must_be_aware')
            t=when(a['transaction_time']);q=number(a['qty']);p=number(a['price']);sym=str(a['symbol']).upper();side=str(a['side']).lower()
            if q<=0 or p<=0 or side not in ('buy','sell'):raise ValueError('invalid_execution')
        except (KeyError,ValueError,TypeError,InvalidOperation):
            invalid.append({'execution_id':ident,'reason':'invalid_execution_fields_or_timestamp','raw_record':a,'decision_status':'execution_with_no_saved_decision_match'});continue
        row={'execution_ids':[ident],'order_id':str(a.get('order_id') or ''),'symbol':sym,'side':side,'quantity':q,'price':p,'first':t,'last':t,'kind':'execution','asset_bucket':'crypto' if '/' in sym or a.get('asset_class')=='crypto' else 'equity','raw':a}
        fills.append(row)
        if row['order_id']:orders[row['order_id']].append(row)
    units={tuple(f['execution_ids']):f for f in fills}
    for oid,rows in orders.items():
        identities={(r['symbol'],r['side'],r['asset_bucket']) for r in rows}
        if len(identities)!=1:raise ValueError('order_identity_conflict')
        rows=sorted(rows,key=lambda r:r['last']);last=rows[-1]
        try:complete=last['raw'].get('order_status')=='filled' and number(last['raw'].get('leaves_qty','NaN'))==0
        except (ValueError,InvalidOperation):complete=False
        if not complete:continue
        ids=tuple(sorted(i for r in rows for i in r['execution_ids']))
        if ids in units:continue
        qty=sum((r['quantity'] for r in rows),Decimal(0));price=sum((r['quantity']*r['price'] for r in rows),Decimal(0))/qty
        units[ids]={'execution_ids':list(ids),'order_id':oid,'symbol':last['symbol'],'side':last['side'],'quantity':qty,'price':price,'first':rows[0]['first'],'last':last['last'],'kind':'completed_order_group','asset_bucket':last['asset_bucket']}
    return fills,list(units.values()),invalid

def price_compatible(journal_price,broker_price):
    j=number(journal_price)
    if j<=0:return False
    precision=Decimal(1).scaleb(j.as_tuple().exponent)
    return broker_price.quantize(precision,rounding=ROUND_HALF_EVEN)==j

def attribute(sources,activities):
    records,metadata=journal_records(sources);fills,units,invalid=fill_units(activities)
    by_key=defaultdict(list)
    for u in units:by_key[(u['symbol'],u['side'])].append(u)
    journal_fills=[r for r in records if r['row']['event']=='fill'];candidate_rows={};rejections=Counter();parsed={}
    for r in journal_fills:
        rid=r['record_id'];row=r['row'];candidates=[]
        if r['ambiguous_same_source_repeat']:
            candidate_rows[rid]=[];rejections['same_source_duplicate']+=1;continue
        try:
            q=number(row['qty']);p=number(row['price']);t=when(row['timestamp'])
            if q<=0 or p<=0:raise ValueError('nonpositive')
        except (KeyError,ValueError,InvalidOperation,TypeError):candidate_rows[rid]=[];rejections['invalid_journal_fill']+=1;continue
        parsed[rid]=(q,p,t)
        for u in by_key[(row.get('symbol','').upper(),row.get('side','').lower())]:
            if abs((t-u['last']).total_seconds())>WINDOW_SECONDS:continue
            if abs(q-u['quantity'])>QTY_TOL:continue
            if not price_compatible(row['price'],u['price']):rejections['price_guard_candidate_rejections']+=1;continue
            candidates.append(u)
        candidate_rows[rid]=candidates
    consumers=defaultdict(set)
    for rid,candidates in candidate_rows.items():
        for u in candidates:
            for ident in u['execution_ids']:consumers[ident].add(rid)
    matches=[];unmatched_journal=[];claimed={}
    contexts=[r for r in records if r['row']['event'] in ('signal','exit')]
    for r in journal_fills:
        rid=r['record_id'];candidates=candidate_rows.get(rid,[])
        if len(candidates)!=1:
            unmatched_journal.append({'record_id':rid,'reason':'ambiguous_candidates' if len(candidates)>1 else 'no_unique_compatible_execution','candidate_count':len(candidates)});continue
        u=candidates[0]
        if any(len(consumers[i])>1 for i in u['execution_ids']):
            unmatched_journal.append({'record_id':rid,'reason':'competing_journal_records','candidate_count':1});continue
        row=r['row'];q,p,t=parsed[rid]
        context=[]
        for c in contexts:
            cr=c['row'];delta=(t-when(cr['timestamp'])).total_seconds()
            same=cr.get('symbol','').upper()==row.get('symbol','').upper() and (not cr.get('side') or cr['side'].lower()==row.get('side','').lower())
            if same and 0<=delta<=WINDOW_SECONDS:
                context.append({'event':cr['event'],'record_id':c['record_id'],'row':cr,'provenance':c['provenance'],'link_status':'nearby_context_not_confirmed_order_link'})
        m={'record_id':rid,'execution_ids':u['execution_ids'],'broker_order_id':u['order_id'],'asset_bucket':u['asset_bucket'],'match_kind':u['kind'],'link_status':'unique_conservative_evidence_match_not_recorded_order_id',
           'time_delta_seconds':(t-u['last']).total_seconds(),'quantity_delta':str(q-u['quantity']),'journal':row,'provenance':r['provenance'],'nearby_signal_exit_context':context,'original_purchase_plan_reconstructed':False}
        matches.append(m)
        for ident in u['execution_ids']:claimed[ident]=rid
    unmatched=[];first=when(metadata['first_journal_timestamp_utc']) if metadata['first_journal_timestamp_utc'] else None
    for f in fills:
        if f['execution_ids'][0] not in claimed:
            unmatched.append({'execution_id':f['execution_ids'][0],'broker_order_id':f['order_id'],'symbol':f['symbol'],'side':f['side'],'quantity':str(f['quantity']),'price':str(f['price']),'transaction_time':f['last'].isoformat(),'asset_bucket':f['asset_bucket'],'decision_status':'execution_with_no_saved_decision_match','before_journal_start':bool(first and f['last'].astimezone(ZONE).date()<first.astimezone(ZONE).date()),'before_first_journal_timestamp':bool(first and f['last']<first)})
    return {'schema':1,'book':'alpaca_paper','journal_metadata':metadata,'decision_records':records,'matches':matches,'unmatched_journal_fills':unmatched_journal,'unmatched_broker_executions':unmatched,
       'summary':{'journal_fill_records':len(journal_fills),'journal_signals':sum(r['row']['event']=='signal' for r in records),'journal_exits':sum(r['row']['event']=='exit' for r in records),'matched_journal_fill_records':len(matches),'matched_broker_executions':len(claimed),'unmatched_journal_fill_records':len(unmatched_journal),'unmatched_broker_executions':len(unmatched),'unmatched_equity_executions':sum(r['asset_bucket']=='equity' for r in unmatched),'unmatched_equity_before_journal_start':sum(r['asset_bucket']=='equity' and r['before_journal_start'] for r in unmatched),'unmatched_equity_before_first_journal_timestamp':sum(r['asset_bucket']=='equity' and r['before_first_journal_timestamp'] for r in unmatched),'matched_equity_journal_fill_records':sum(m['asset_bucket']=='equity' for m in matches),'matched_crypto_journal_fill_records':sum(m['asset_bucket']=='crypto' for m in matches),'invalid_broker_executions':len(invalid),'candidate_rejection_diagnostics':dict(rejections)},
       'invalid_broker_records':invalid,'whole_book_profit':None,'whole_book_profit_status':'BLOCKED_UNKNOWN_OPENING_INVENTORY_AND_UNAPPLIED_CORPORATE_ACTIONS','assumptions':['Naive journal timestamps interpreted as America/Chicago; original offsets were not saved.','Journal evidence attribution is not a reconstructed original plan or verified opening cost basis.','Signal/exit context without a recorded order ID remains explicitly unconfirmed.']}

def cost_buckets(activities):
    buckets=defaultdict(lambda:defaultdict(lambda:{'count':0,'net_amount':Decimal(0)}));unclassified=[];seen={}
    for a in activities:
        if a.get('activity_type')=='FILL':continue
        ident=str(a.get('id') or '')
        if not ident:raise ValueError('nontrade_activity_id_required')
        sig=json.dumps(a,sort_keys=True)
        if ident in seen:
            if seen[ident]!=sig:raise ValueError('conflicting_nontrade_duplicate')
            continue
        seen[ident]=sig;kind=a.get('activity_type');sub=a.get('activity_sub_type');status=a.get('status')
        if status not in ('executed','correct'):
            unclassified.append({'activity_id':ident,'reason':'not_confirmed_posted'});continue
        if kind=='FEE' and sub in ('REG','CAT','TAF'):bucket='equity_regulatory_'+sub
        elif kind=='FEE':bucket='unclassified_fee_requires_review'
        elif kind=='CFEE':bucket='crypto_pair_transaction_fee'
        elif kind=='DIV':bucket='posted_dividend_cash'
        elif kind in ('CSD','CSW','JNLC','JNLS'):bucket='cash_movement_requires_reconciliation'
        else:bucket='corporate_or_other_activity_requires_reconciliation'
        currency=str(a.get('currency') or 'UNKNOWN')
        try:amount=number(a['net_amount'])
        except (KeyError,ValueError,InvalidOperation):unclassified.append({'activity_id':ident,'reason':'invalid_net_amount'});continue
        slot=buckets[bucket][currency];slot['count']+=1;slot['net_amount']+=amount
    output={k:{c:{'count':v['count'],'net_amount':str(v['net_amount'])} for c,v in currencies.items()} for k,currencies in buckets.items()}
    return {'buckets':output,'unclassified':unclassified,'equity_commission_inferred':False,'crypto_fees_in_equity_costs':False,'dividend_strategy_added':False,'whole_book_profit':None,'note':'Signed posted amounts by currency; regulatory pass-throughs and crypto fees stay separate. No inferred dividend cash or cost-basis profit.'}
