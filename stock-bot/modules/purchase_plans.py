"""Observational pre-submit equity plans. No broker calls or strategy decisions."""
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import threading
import uuid

_log=logging.getLogger(__name__)
_lock=threading.Lock()
BOOKS={'alpaca_live','alpaca_paper_v2','alpaca_paper'}

def _number(value):
    try:
        n=float(value)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError): return None

def _value(cfg,name,default=None):
    value=getattr(cfg,name,default)
    return value() if callable(value) else value

def exit_rules(cfg):
    """Read effective settings only; never resolve ATR, quotes, safety, or scanners."""
    if _value(cfg,'paper_lab_concentrated_enabled',False):
        return dict(kind='lab',disaster_fraction=_value(cfg,'paper_lab_disaster_pct'),
                    trail_arm_fraction=_value(cfg,'paper_lab_trail_arm_pct'),
                    trail_fraction=_value(cfg,'paper_lab_trail_pct'),
                    half_gain_fraction=_value(cfg,'paper_lab_half_gain_pct'),
                    max_hold=_value(cfg,'paper_lab_max_hold_days'),hold_units='implementation_days',
                    a2b1=_value(cfg,'paper_nyse_a2b1_enabled'),
                    future_stop_price=None)
    if _value(cfg,'effective_smart_stops_enabled',False):
        return dict(kind='dynamic_smart_atr',atr_multiplier=_value(cfg,'ATR_STOP_MULTIPLIER'),
                    atr_period=_value(cfg,'ATR_PERIOD'),atr_definition='daily_mean_absolute_close_change',
                    reeval_fractions=_value(cfg,'STOP_LOSS_REEVAL_PCTS'),
                    max_hold=_value(cfg,'PAPER_POSITION_MAX_HOLD_BARS') if _value(cfg,'paper_medium_strategy_enabled',False) else None,
                    hold_units='implementation_age_bars',future_stop_price=None,
                    limitation='ATR and in-process stop must be read from contemporaneous stop evidence, not recomputed for this plan')
    if _value(cfg,'effective_exit_optimization_enabled',False):
        return dict(kind='exit_optimization',future_stop_price=None,max_hold=None,
                    limitation='Dynamic optimizer details unavailable in this snapshot')
    return dict(kind='fixed_entry_stop',loss_fraction=_value(cfg,'STOP_LOSS_PCT'),
                max_hold=None,holding_period='no time exit in this position-exit path',
                future_stop_price=None)

def rank_context(symbol):
    # Link the original ranking tape. No second call to any scanner.
    try:
        from modules.decision_tape import _active
        state=_active.get()
        if not state: return None
        context={'decision_id':state['id'],'base_score':None,'final_score':None,'boosts':{}}
        for raw in reversed(state.get('buffer',[])):
            row=json.loads(raw)
            if row['event']=='ranked' and context['final_score'] is None:
                context['final_score']=next((v for v,s in row.get('scores',[]) if s==symbol),None)
            if row['event']=='boost' and row.get('symbol')==symbol:
                context['boosts'].setdefault(row['component'],row.get('applied_value'))
            if row['event']=='candidate' and row.get('symbol')==symbol:
                context.update(base_score=row.get('base_score'),closes=row.get('closes'),window=row.get('window'),eligible=row.get('eligible'))
                break
        return context
    except Exception: return None

def _append(book,event,**payload):
    # A small append-only file per book/day. Do not serialize an executor/config/env.
    path=Path(__file__).resolve().parents[1]/'data'/'trade_plans'/book/(datetime.now(timezone.utc).date().isoformat()+'.jsonl')
    row=dict(schema=1,book=book,event=event,observed_at=datetime.now(timezone.utc).isoformat(),**payload)
    blob=json.dumps(row,allow_nan=False,default=str,separators=(',',':'))+'\n'
    with _lock:
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and path.stat().st_size+len(blob.encode('utf8'))>32*1024*1024:
            raise OSError('daily plan limit')
        with path.open('a',encoding='utf8') as f: f.write(blob)

def before_submit(executor,cfg,*,symbol,side,reason,sleeve,order):
    """Best-effort only: return correlation ID, never prevent a broker operation."""
    try:
        book=os.environ.get('TRADING_BOOK_ID','')
        if book not in BOOKS or str(side).lower()!='buy' or getattr(executor,'dry_run',False): return None
        if cfg.is_crypto(symbol): return None
        positions=getattr(executor,'_positions',None)
        p=next((p for p in positions or [] if str(p.symbol).replace('/','')==str(symbol).replace('/','')),None)
        qty=_number(getattr(p,'qty',None)) if p is not None else (0.0 if positions is not None else None)
        avg=_number(getattr(p,'avg_entry_price',None)) if p is not None else None
        rules=exit_rules(cfg)
        plan_id=uuid.uuid4().hex
        stop=avg*(1-rules['loss_fraction']) if avg and rules.get('kind')=='fixed_entry_stop' and rules.get('loss_fraction') is not None else None
        _append(book,'purchase_plan',plan_id=plan_id,symbol=symbol,side='buy',reason=reason,sleeve=sleeve,
                attribution='NYSE_MA50' if '/MA50' in str(reason) else 'other_or_unverified',
                selection=rank_context(symbol),requested_notional=_number(getattr(order,'notional',None)),
                requested_quantity=_number(getattr(order,'qty',None)),
                role=('cover_short' if qty is not None and qty<0 else 'add' if qty else 'new' if qty==0 else 'unknown'),
                before=dict(quantity=qty,average_entry=avg,stop_price=stop,evidence='cached_position_age_unknown'),
                exit_rules=rules,fill_price=None,after_fill_average=None,after_fill_stop=None,
                post_fill_status='requires_broker_fill_reconciliation',
                plan_complete=False,completion_status='pre_submit_rules_only_fill_and_adherence_pending',
                limitations=['No price prediction or expected return implied','Other sleeve exits/portfolio guards can also sell','Cached inventory is not fresh broker evidence'])
        return (book,plan_id)
    except Exception as exc:
        _log.warning('TRADE PLAN INCOMPLETE: %s',type(exc).__name__)
        return None

def submitted(token,order):
    try:
        if token is None: return
        book,plan_id=token
        oid=getattr(order,'id',None)
        _append(book,'order_link',plan_id=plan_id,order_id=str(oid) if oid else None,
                confirmed_fill=False,status=str(getattr(order,'status','unknown')))
    except Exception as exc:
        _log.warning('TRADE PLAN INCOMPLETE: %s',type(exc).__name__)
