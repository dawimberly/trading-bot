"""Offline evidence review. No broker, credentials, trading imports or deployment hooks."""
from __future__ import annotations
import argparse, hashlib, json, math
from datetime import datetime, timezone
from pathlib import Path

BOOKS=('alpaca_live','alpaca_paper_v2','alpaca_paper')
TARGET=.50

def stamp(value):
    t=datetime.fromisoformat(value.replace('Z','+00:00'))
    if t.tzinfo is None: raise ValueError('timezone required')
    return t.astimezone(timezone.utc)

def finite(value):
    return isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value)

def annual_target(net_return, sessions, cashflows_reconciled):
    # Do not turn a short winning stretch into a claimed annual result.
    if not cashflows_reconciled or not finite(net_return) or net_return < -1 or not isinstance(sessions,int) or isinstance(sessions,bool) or sessions < 252:
        return {'status':'unverified','cagr':None,'target':TARGET}
    cagr=(1+net_return)**(252/sessions)-1
    return {'status':'met_historically' if cagr>=TARGET else 'below_target','cagr':cagr,'target':TARGET,'sessions':sessions,'forward_guarantee':False}

def review_book(report, expected_book, now):
    if report.get('book')!=expected_book: raise ValueError('book identity mismatch')
    if report.get('schema')!=1: raise ValueError('unsupported report schema')
    when=stamp(report['as_of']);age=(now-when).total_seconds()/3600
    if age < -1/60: raise ValueError('future report')
    c=report.get('coverage') or {};p=report.get('purchases') or [];s=report.get('sales') or []
    planned=sum(bool(x.get('original_plan')) for x in p)
    complete_sales=[x for x in s if x.get('complete_30_sessions') is True and x.get('elapsed_sessions',0)>=30]
    price_moves=[]
    for x in complete_sales:
        m=(x.get('milestones') or {}).get('30') or {}
        try: available=stamp(m['session']+'T23:59:59+00:00')<=now
        except (KeyError,ValueError,TypeError): available=False
        if available and finite(m.get('price_return')) and not m.get('unavailable_reasons'): price_moves.append(m['price_return'])
    gaps=[]
    if report.get('error'): gaps.append('report_error')
    if report.get('decision_record_attribution_error'): gaps.append('legacy_journal_attribution_unavailable')
    if c.get('action_scoping_unverified_symbols'): gaps.append('action_scoping_unverified')
    if c.get('snapshot_errors'): gaps.append('snapshot_errors')
    if age>72: gaps.append('stale_report_over_72h')
    if c.get('complete_from_start') is not True or c.get('truncated') is not False: gaps.append('incomplete_fill_history')
    if c.get('initial_inventory') not in ('verified','known','empty_verified'): gaps.append('unresolved_initial_inventory_or_corporate_actions')
    if planned<len(p): gaps.append('original_purchase_plans_missing')
    if c.get('sale_symbols_deferred'): gaps.append('post_sale_symbols_deferred')
    for key in ('pagination_error','conflicts','price_errors','positions_error','calendar_error'):
        if c.get(key): gaps.append(key)
    # Raw fill IDs, account hashes, exact quantities and balances are never copied.
    return {'book':expected_book,'as_of':report['as_of'],'age_hours':round(age,2),
        'purchases':len(p),'sales':len(s),'plans_available':planned,
        'fills_stored':c.get('fills_stored'),'complete_fill_pagination':c.get('complete_from_start') is True and c.get('truncated') is False,
        'post_sale_30_price_observations':len(price_moves),
        'post_sale_30_mean_price_return':sum(price_moves)/len(price_moves) if price_moves else None,
        'post_sale_note':'sale-level price-only observations, including partial/unknown inventory; correlated, not net replacement returns or timing verdicts',
        'verified_full_exits':sum(x.get('position_effect')=='full_exit' for x in s),
        'gaps':gaps,'performance_target':{'status':'unverified','target':TARGET,'reason':'broker cash-flow-adjusted equity series not supplied'},
        'aspects':{
            'entry_and_selection':'original plans counted; full as-of rejected-candidate outcomes not supplied',
            'execution':'fill pagination checked; decision-to-fill quote/spread evidence not supplied',
            'exit_and_followthrough':'mature 30-session price paths counted; no hindsight good/bad sale label',
            'position_and_portfolio_risk':'no verified equity, exposure, sector or concentration series supplied',
            'costs_and_cashflows':'fees, dividends, deposits, withdrawals and corporate actions require reconciled ledgers',
            'regime_and_opportunity_cost':'not measurable from fill-only report',
            'plan_adherence':'requires original plan, decision state and linked broker fills'},
        'legacy_decision_record_coverage':{k:v for k,v in (report.get('decision_record_coverage') or {}).items() if k in ('journal_fill_records','journal_signals','journal_exits','matched_journal_fill_records','matched_broker_executions','unmatched_journal_fill_records','unmatched_broker_executions','unmatched_equity_executions','unmatched_equity_before_journal_start','unmatched_equity_before_first_journal_timestamp')},
        'strategy_promotion':False}

def review_experiment(result):
    # This adapter supports the completed ranking-learner development diagnostic only.
    if result.get('label')!='PREVIOUSLY EXPOSED DEVELOPMENT DIAGNOSTIC': raise ValueError('unsupported experiment schema')
    gates=result.get('gates') or {};required=('three_folds','net_return','drawdown','first_half','second_half','higher_cost','no_engine_action_blocks')
    passing=all(gates.get(k) is True for k in required)
    out={'name':'Lab Next ranking learner v1','family':'adaptive_ranking','status':'development_screen_pass_only' if passing else 'reject',
         'failed_gates':[k for k in required if gates.get(k) is not True], 'promotion':False,'annual_target':{'status':'unverified','target':TARGET,'reason':'short, previously exposed development history'},'metrics':{}}
    for cost,key in [('5bps','base_cost'),('10bps','higher_cost')]:
        stats=result.get(key,{}).get('stats',{})
        out['metrics'][cost]={n:{k:v.get(k) for k in ('full_fold_return','full_fold_maxdd','all_147_return')} for n,v in stats.items() if n in ('baseline','learner')}
    return out

def build(inputs, now, experiment=None):
    books=[];errors=[]
    for book in BOOKS:
        if book not in inputs:
            errors.append({'book':book,'error':'missing_report'});continue
        try: books.append(review_book(inputs[book],book,now))
        except (ValueError,KeyError,TypeError) as exc: errors.append({'book':book,'error':str(exc)})
    return {'schema':1,'generated_at':now.isoformat(),'goal':{'net_annual_return':TARGET,'costs':'net of trading fees and slippage, before personal taxes','guarantee':False},
        'status':'EVIDENCE_GAPS' if errors or any(b['gaps'] for b in books) else 'REVIEW_ONLY',
        'books':books,'errors':errors,'experiments':[review_experiment(experiment)] if experiment else [],
        'best_strategy':None,'best_strategy_reason':'no independently validated strategy satisfying comparable return, risk, execution and protocol gates supplied',
        'automatic_deployment':False,'new_data_cost_dollars':0}

def markdown(r):
    lines=['# Automated trading research review','',f"Generated: {r['generated_at']}",'','Objective: 50% net annual return; research target, not a promise or a risk override.','',f"Status: {r['status']}",'','| Book | Report as of | Purchases | Plans | Sales | Mature 30-session price paths |','|---|---|---:|---:|---:|---:|']
    for b in r['books']: lines.append(f"| {b['book']} | {b['as_of']} | {b['purchases']} | {b['plans_available']} | {b['sales']} | {b['post_sale_30_price_observations']} |")
    lines+=['','## Evidence gaps']
    for b in r['books']: lines.append('- '+b['book']+': '+(', '.join(b['gaps']) or 'No basic coverage flags; this is not performance validation.'))
    for e in r['errors']:lines.append('- '+e['book']+': '+e['error'])
    lines+=['','## Strategy competition']
    for e in r['experiments']:lines.append('- '+e['name']+': '+e['status']+'; failed gates: '+', '.join(e['failed_gates'])+'. No promotion.')
    lines+=['','No best strategy has been established. None of the supplied book reports proves the annual target. Missing values are not zero. Post-sale price changes do not establish that a sale was wrong; they exclude fees, dividends, risk and replacement capital.','','The structured JSON lists coverage for entry, execution, exits, sizing, risk, costs, regimes and plan adherence. This runner reads prepared reports only. It neither collects fresh broker data nor trains models, trades, schedules itself or changes any book.']
    return '\n'.join(lines)+'\n'

def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--reports',type=Path,required=True);a.add_argument('--output',type=Path,required=True);a.add_argument('--experiment',type=Path);a.add_argument('--as-of');args=a.parse_args()
    now=stamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
    inputs={};manifest={}
    for book in BOOKS:
        p=args.reports/(book+'.json')
        if p.exists():
            raw=p.read_bytes();manifest[book]=hashlib.sha256(raw).hexdigest();inputs[book]=json.loads(raw)
    experiment=None
    if args.experiment:
        raw=args.experiment.read_bytes();manifest['experiment']=hashlib.sha256(raw).hexdigest();experiment=json.loads(raw)
    r=build(inputs,now,experiment);r['input_sha256']=manifest
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'review.json').write_text(json.dumps(r,indent=2,allow_nan=False),encoding='utf8')
    (args.output/'review.md').write_text(markdown(r),encoding='utf8')
    print(json.dumps({'status':r['status'],'books':len(r['books']),'best_strategy':None,'automatic_deployment':False}))
    return 0
if __name__=='__main__':raise SystemExit(main())
