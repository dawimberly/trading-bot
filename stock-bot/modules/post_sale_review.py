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


def session_date(value):
    from datetime import date
    value=str(value)
    if date.fromisoformat(value).isoformat()!=value: raise ValueError('canonical ISO session date required')
    return value

def purchase_fill_details(*,book,group,plan,snapshots,groups,as_of):
    out=dict(quantity=float(group['qty']),fill_vwap=float(group['notional']/group['qty']),
             broker_average_entry_after=None,broker_quantity_after=None,
             rule_implied_stop_after_fill=None,stop_is_broker_order=False,
             snapshot_status='no_verified_post_fill_snapshot')
    next_times=[g['first'] for g in groups if g['symbol']==group['symbol'] and g['order_id']!=group['order_id'] and g['first']>group['last']]
    next_time=min(next_times) if next_times else None
    valid=[]
    for row in snapshots:
        if row.get('book')!=book or row.get('after_order_id')!=group['order_id'] or row.get('symbol')!=group['symbol'] or row.get('source')!='alpaca_positions':continue
        try:
            when=stamp(row['observed_at'])
            if when>as_of or when<group['last'] or (next_time is not None and when>=next_time):continue
            qty=positive(row['qty']);avg=positive(row['avg_entry_price'])
            valid.append((when,qty,avg))
        except (KeyError,ValueError,TypeError):continue
    # A same-symbol order executing across the snapshot interval invalidates it.
    if any(g['order_id']!=group['order_id'] and g['symbol']==group['symbol'] and g['first']<=group['last']<=g['last'] for g in groups):
        valid=[]
    if not valid:return out
    latest=max(t for t,q,p in valid)
    at_latest={(q,p) for t,q,p in valid if t==latest}
    if len(at_latest)!=1:
        out['snapshot_status']='conflicting_post_fill_snapshots';return out
    qty,avg=at_latest.pop()
    out.update(broker_average_entry_after=avg,broker_quantity_after=qty,
               snapshot_status='attributable_caller_supplied_broker_snapshot',snapshot_at=latest.isoformat())
    rules=(plan or {}).get('exit_rules',{})
    fraction=rules.get('loss_fraction')
    if rules.get('kind')=='fixed_entry_stop' and isinstance(fraction,(float,int)) and math.isfinite(fraction) and 0<=fraction<1:
        out['rule_implied_stop_after_fill']=avg*(1-fraction)
    return out


def post_sale_report(*,book,fills,calendar,prices,as_of,plans=(),initial_inventory=None,position_snapshots=()):
    """Prices metadata explicitly declares unit alignment and action coverage.
    Calendar rows contain date and timezone-aware close. No network or config imports.
    """
    if book not in ('alpaca_live','alpaca_paper_v2','alpaca_paper'): raise ValueError('explicit book required')
    as_of=stamp(as_of)
    sessions=[];all_sessions=[]
    for row in calendar:
        day=session_date(row['date']);close=stamp(row['close'])
        if close.astimezone(ZoneInfo('America/New_York')).date().isoformat()!=day:
            raise ValueError('calendar date disagrees with exchange-local close')
        all_sessions.append(day)
        if close<=as_of:sessions.append(day)
    if all_sessions!=sorted(set(all_sessions)):raise ValueError('unique ascending exchange sessions required')
    plan_by_id={};ambiguous=set();links={}
    for row in plans:
        if row.get('book')!=book or row.get('event')!='purchase_plan' or not row.get('plan_id'):continue
        key=row['plan_id']
        if key in plan_by_id and plan_by_id[key]!=row:ambiguous.add(key)
        plan_by_id[key]=row
    link_ids={}
    for row in plans:
        if row.get('book')!=book or row.get('event')!='order_link' or not row.get('order_id'):continue
        try:
            if stamp(row['observed_at'])>as_of:continue
        except (KeyError,ValueError,TypeError):continue
        link_ids.setdefault(row['order_id'],set()).add(row.get('plan_id'))
    for oid,ids in link_ids.items():
        if len(ids)==1:
            ident=next(iter(ids))
            if ident not in ambiguous:links[oid]=plan_by_id.get(ident)
    inventory={s:Decimal(str(q)) for s,q in (initial_inventory or {}).items()}
    if any(not q.is_finite() for q in inventory.values()): raise ValueError('finite initial inventory required')
    buys=[];sales=[]
    groups=group_fills([f for f in fills if str(f.get("activity_type","FILL"))=="FILL" and stamp(f["transaction_time"])<=as_of])
    for g in groups:
        sym=g['symbol'];q=g['qty'];before=inventory.get(sym,Decimal(0))
        overlapping=any(o["symbol"]==sym and o["order_id"]!=g["order_id"] and o["first"]<=g["last"] and o["last"]>=g["first"] for o in groups)
        verified=initial_inventory is not None and not overlapping
        price=float(g['notional']/q)
        inventory[sym]=before+q if g['side']=='buy' else before-q
        basic=dict(order_id=g['order_id'],symbol=sym,quantity=float(q),fill_price=price,
                   filled_at=g['last'].isoformat(),execution_count=g['executions'],
                   order_partial_fill_is_not_position_partial=True)
        if g['side']=='buy':
            plan=links.get(g['order_id'])
            try:
                if plan and (stamp(plan.get('observed_at',''))>g['first'] or plan.get('symbol')!=sym or plan.get('side')!='buy'): plan=None
            except (ValueError,TypeError):plan=None
            buys.append(dict(basic,original_plan=plan,plan_status='contemporaneous_plan_linked' if plan else 'original_plan_unavailable',
                             position_role=('cover_short' if before<0 else 'add' if before>0 else 'new') if verified else 'unknown_inventory',
                             filled_plan=purchase_fill_details(book=book,group=g,plan=plan,snapshots=position_snapshots,groups=groups,as_of=as_of),
                             plan_adherence='not_evaluated_without_decision_and_exit_evidence'))
            continue
        day=g['last'].astimezone(ZoneInfo('America/New_York')).date().isoformat()
        calendar_ok=day in all_sessions
        after=[d for d in sessions if d>day][:30] if calendar_ok else []
        data=prices.get(sym,{})
        bars={}
        for row in data.get('bars',[]):
            bar_date=session_date(row['date'])
            if bar_date in bars:raise ValueError('duplicate daily price date')
            bars[bar_date]=row
        bars={d:b for d,b in bars.items() if d in sessions}

        units_ok=data.get('basis')=='raw_same_units_as_broker' and data.get('unit_alignment_verified') is True
        try:
            actions_from=session_date(data.get('corporate_actions_checked_from',''))
            actions_through=session_date(data.get('corporate_actions_checked_through',''))
        except ValueError:
            actions_from='9999-12-31';actions_through='0001-01-01'
        unknown=[session_date(d) for d in data.get('uncertain_action_dates',[])]
        changing=data.get('position_changing_action_dates')
        actions_verified=isinstance(changing,list) and actions_from<=day<=actions_through
        changing=[session_date(d) for d in changing or []]

        milestones={}
        for h in HORIZONS:
            target=after[h-1] if len(after)>=h else None
            bar=bars.get(target) if target else None
            reasons=[]
            if not calendar_ok:reasons.append('calendar_missing_sale_session')
            if target is None: reasons.append('not_mature')
            if target and not bar: reasons.append('missing_exact_session_bar')
            if not units_ok: reasons.append('price_units_unverified')
            if target and (not actions_verified or actions_through<target): reasons.append('corporate_action_coverage_unverified')
            if target and any(day<d<=target for d in unknown): reasons.append('corporate_action_uncertain')
            if target and any(day<d<=target for d in changing):reasons.append('position_changing_corporate_action')
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
        extrema_ok=units_ok and actions_verified and after and actions_through>=after[-1] and not any(day<d<=after[-1] for d in unknown+changing)
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
                          extrema_complete_for_elapsed_sessions=len(usable)==len(after) and bool(after),
                          highest_move=max((hi/price-1 for _,hi,_ in usable),default=None) if extrema_ok else None,
                          lowest_move=min((lo/price-1 for _,_,lo in usable),default=None) if extrema_ok else None,
                          comparison='hypothetical continued hold of sold shares; price only',
                          dividends='not_included',costs='not_included; no assumed resale fee',
                          replacement_capital_result=None,replacement_status='not_attributable_from_next_buy_alone',
                          adherence='unknown_without_linked_original_plan_and_exit_decision',timing_verdict='not_assigned_from_hindsight'))
    return dict(schema=1,book=book,as_of=as_of.isoformat(),account_identity='caller_supplied_not_independently_verified',
                purchases=buys,sales=sales,scope='observational_only',session_definition='exchange sessions strictly after final execution Eastern sale date')


def render_markdown(report):
    """Human-readable review. Never presents post-sale returns as net strategy profit."""
    def safe(value):return str(value).replace('|','/').replace('\n',' ').replace('\r',' ')
    def number(value,percent=False):
        if value is None:return 'N/A'
        return f'{value*100:+.2f}%' if percent else f'{value:.4f}'
    lines=[f"# Purchase plans and sale follow-through — {safe(report['book'])}",
           f"As of: {safe(report['as_of'])}",
           "Price-only observations; fees and dividends excluded. No hindsight timing verdict or inferred redeployment profit.",
           "",'## Purchases','',
           '| Symbol | Quantity | Fill VWAP | Original plan | Broker avg after fill | Rule-implied stop after fill |',
           '|---|---:|---:|---|---:|---:|']
    for row in report['purchases']:
        filled=row.get('filled_plan',{})
        lines.append('| '+ ' | '.join([safe(row['symbol']),number(row['quantity']),number(row['fill_price']),safe(row['plan_status']),number(filled.get('broker_average_entry_after')),number(filled.get('rule_implied_stop_after_fill'))])+' |')
    lines+=['','## Open positions','','A current holding is not a sale. Older sale rows stay in the sales table.','']
    for row in report.get('open_positions') or []:
        lines.append(safe(row.get('symbol'))+' qty '+safe(row.get('quantity'))+' — sale follow-through: '+safe(row.get('sale_follow_through'))+'. Original plan unavailable.')
    if not report.get('open_positions'):
        lines.append(safe(report.get('open_position_note') or 'No current broker position in this report.'))
    lines+=['','## Sales','',
            '| Symbol | Position effect | Sale VWAP | +1 session | +5 | +10 | +20 | +30 | Complete 30-session path |',
            '|---|---|---:|---:|---:|---:|---:|---:|---|']
    notes=[]
    for row in report['sales']:
        parts=[safe(row['symbol']),safe(row['position_effect']),number(row['fill_price'])]
        parts += [number(row['milestones'][str(h)]['price_return'],True) for h in HORIZONS]
        parts += ['yes' if row['complete_30_sessions'] else 'no']
        lines.append('| '+' | '.join(parts)+' |')
        missing=sorted({reason for m in row['milestones'].values() for reason in m['unavailable_reasons']})
        if missing:notes.append(safe(row['symbol'])+' unavailable/gaps: '+safe(', '.join(missing)))
        notes.append(safe(row['symbol'])+' extrema ('+safe(row['extrema_basis'])+'): '+number(row['lowest_move'],True)+' to '+number(row['highest_move'],True)+'; observed '+str(row['observed_sessions'])+' of '+str(row['elapsed_sessions'])+' elapsed sessions.')
    lines+=['']+notes
    lines+=['','Plan adherence requires original decision/exit evidence. Broker average snapshots are caller-supplied, not independently authenticated by this offline tool.']
    return '\n'.join(lines)+'\n'
