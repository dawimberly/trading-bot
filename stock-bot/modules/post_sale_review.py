"""Offline, broker-fill based 30-session post-sale observations; never a trading signal."""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
import math

HORIZONS=(1,5,10,20,30)

def stamp(value):
    dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('timezone-aware broker timestamp required')
    return dt

def positive(value):
    n=float(value)
    if not math.isfinite(n) or n<=0: raise ValueError('positive finite price/quantity required')
    return n

def group_fills(fills):
    """Deduplicate executions by broker activity ID; aggregate partial executions/order."""
    seen={};groups={}
    for f in fills:
        if str(f.get('activity_type','FILL'))!='FILL': continue
        ident=f.get('id')
        if not ident: raise ValueError('broker execution ID required')
        signature=(f.get('order_id'),f.get('symbol'),f.get('side'),f.get('qty'),f.get('price'),f.get('transaction_time'))
        if ident in seen:
            if seen[ident]!=signature: raise ValueError('conflicting duplicate execution')
            continue
        seen[ident]=signature
        oid=f.get('order_id');sym=f.get('symbol');side=str(f.get('side','')).lower()
        if not oid or not sym or side not in ('buy','sell'): raise ValueError('order identity required')
        dt=stamp(f['transaction_time']);positive(f['qty']);positive(f['price'])
        q=Decimal(str(f['qty']));p=Decimal(str(f['price']))
        g=groups.setdefault(oid,dict(order_id=oid,symbol=sym,side=side,qty=Decimal(0),notional=Decimal(0),first=dt,last=dt,executions=0))
        if (g['symbol'],g['side'])!=(sym,side): raise ValueError('order reused across symbols/sides')
        g['qty']+=q;g['notional']+=q*p;g['first']=min(g['first'],dt);g['last']=max(g['last'],dt);g['executions']+=1
    return sorted(groups.values(),key=lambda g:(g['last'],g['order_id']))

def post_sale_report(*,book,fills,calendar,prices,as_of,plans=(),initial_inventory=None):
    """Prices metadata explicitly declares unit alignment and action coverage.
    Calendar rows contain date and timezone-aware close. No network or config imports.
    """
    if book not in ('alpaca_live','alpaca_paper_v2','alpaca_paper'): raise ValueError('explicit book required')
    as_of=stamp(as_of)
    sessions=[]
    for row in calendar:
        close=stamp(row['close'])
        if close<=as_of: sessions.append(row['date'])
    if sessions!=sorted(set(sessions)): raise ValueError('unique ascending exchange sessions required')
    plan_by_id={r['plan_id']:r for r in plans if r.get('book')==book and r.get('event')=='purchase_plan'}
    links={r['order_id']:plan_by_id.get(r.get('plan_id')) for r in plans if r.get('book')==book and r.get('event')=='order_link' and r.get('order_id')}
    inventory={s:Decimal(str(q)) for s,q in (initial_inventory or {}).items()}
    buys=[];sales=[]
    groups=group_fills([f for f in fills if str(f.get("activity_type","FILL"))=="FILL" and stamp(f["transaction_time"])<=as_of])
    for g in groups:
        sym=g['symbol'];q=g['qty'];before=inventory.get(sym,Decimal(0))
        overlapping=any(o["symbol"]==sym and o["order_id"]!=g["order_id"] and o["first"]<g["last"] and o["last"]>g["first"] for o in groups)
        verified=initial_inventory is not None and not overlapping
        price=float(g['notional']/q)
        inventory[sym]=before+q if g['side']=='buy' else before-q
        basic=dict(order_id=g['order_id'],symbol=sym,quantity=float(q),fill_price=price,
                   filled_at=g['last'].isoformat(),execution_count=g['executions'],
                   order_partial_fill_is_not_position_partial=True)
        if g['side']=='buy':
            plan=links.get(g['order_id'])
            try:
                if plan and stamp(plan.get('observed_at',''))>g['first']: plan=None
            except (ValueError,TypeError):plan=None
            buys.append(dict(basic,original_plan=plan,plan_status='contemporaneous_plan_linked' if plan else 'original_plan_unavailable',
                             position_role=('cover_short' if before<0 else 'add' if before>0 else 'new') if verified else 'unknown_inventory',
                             plan_adherence='not_evaluated_without_decision_and_exit_evidence'))
            continue
        day=g['last'].astimezone(ZoneInfo('America/New_York')).date().isoformat()
        after=[d for d in sessions if d>day][:30]
        data=prices.get(sym,{})
        bars={r['date']:r for r in data.get('bars',[]) if r['date']<=as_of.astimezone(ZoneInfo('America/New_York')).date().isoformat()}
        units_ok=data.get('basis')=='raw_same_units_as_broker' and data.get('unit_alignment_verified') is True
        actions_through=data.get('corporate_actions_checked_through','')
        unknown=data.get('uncertain_action_dates',[])
        milestones={}
        for h in HORIZONS:
            target=after[h-1] if len(after)>=h else None
            bar=bars.get(target) if target else None
            reasons=[]
            if target is None: reasons.append('not_mature')
            if target and not bar: reasons.append('missing_exact_session_bar')
            if not units_ok: reasons.append('price_units_unverified')
            if target and actions_through<target: reasons.append('corporate_action_coverage_unverified')
            if target and any(day<d<=target for d in unknown): reasons.append('corporate_action_uncertain')
            value=None
            if not reasons:
                try:value=positive(bar['close'])/price-1
                except (ValueError,TypeError,KeyError):reasons.append('invalid_close')
            milestones[str(h)]=dict(session=target,price_return=value,
                                    hypothetical_hold_price_change_dollars=float(q)*price*value if value is not None else None,
                                    unavailable_reasons=reasons)
        usable=[];intraday=True
        for d in after:
            b=bars.get(d)
            if b is None: continue
            try:
                c=positive(b['close']);hi=positive(b['high']) if 'high' in b else c;lo=positive(b['low']) if 'low' in b else c
                if lo>c or hi<c or lo>hi: continue
                if 'high' not in b or 'low' not in b: intraday=False
                usable.append((d,hi,lo))
            except (ValueError,TypeError,KeyError):continue
        extrema_ok=units_ok and after and actions_through>=after[-1] and not any(day<d<=after[-1] for d in unknown)
        full=(len(after)==30 and len(usable)==30 and not milestones['30']['unavailable_reasons'])
        effect='unknown_inventory'
        if verified:
            effect='full_exit' if before>0 and inventory[sym]==0 else 'partial_position_sale' if before>q else 'short_open_or_mixed' if q>before else 'unknown'
        if effect=='short_open_or_mixed':
            for milestone in milestones.values():
                milestone['price_return']=None
                milestone['hypothetical_hold_price_change_dollars']=None
                milestone['unavailable_reasons'].append('not_a_verified_long_position_sale')
            extrema_ok=False;full=False
        sales.append(dict(basic,sale_date_et=day,position_effect=effect,remaining_quantity=float(inventory[sym]) if verified else None,
                          milestones=milestones,observed_sessions=len(usable),elapsed_sessions=len(after),complete_30_sessions=bool(full),
                          extrema_basis='daily_high_low' if intraday else 'daily_close_only',
                          highest_move=max((hi/price-1 for _,hi,_ in usable),default=None) if extrema_ok else None,
                          lowest_move=min((lo/price-1 for _,_,lo in usable),default=None) if extrema_ok else None,
                          comparison='hypothetical continued hold of sold shares; price only',
                          dividends='not_included',costs='not_included; no assumed resale fee',
                          replacement_capital_result=None,replacement_status='not_attributable_from_next_buy_alone',
                          adherence='unknown_without_linked_original_plan_and_exit_decision',timing_verdict='not_assigned_from_hindsight'))
    return dict(schema=1,book=book,as_of=as_of.isoformat(),account_identity='caller_supplied_not_independently_verified',
                purchases=buys,sales=sales,scope='observational_only',session_definition='exchange sessions strictly after final execution Eastern sale date')
