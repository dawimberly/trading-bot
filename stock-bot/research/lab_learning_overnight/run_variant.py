"""Offline-only ranking-learner study. Never imports a broker or collector."""
import offline_guard
import argparse,hashlib,json,math,sys,time
from pathlib import Path
from datetime import time as clock,datetime,timedelta
ROOT=Path(__file__).resolve().parent

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def pinned(base,ref):
 p=(base/ref['path']).resolve()
 if digest(p)!=ref['sha256']:raise ValueError('input hash mismatch: '+p.name)
 return p

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--manifest',type=Path,required=True);ap.add_argument('--output',required=True);ap.add_argument('--cost-bps',type=int,choices=[5,10],default=5);ap.add_argument('--variant',choices=['pairwise_rank','median_linear','local_expectancy'],required=True);args=ap.parse_args()
 if not args.output.replace('_','').replace('-','').isalnum():raise ValueError('simple output name required')
 out=ROOT/'results'/args.output
 if out.exists():raise ValueError('refusing to overwrite an earlier study')
 frozen=json.loads((ROOT/'preregistration.json').read_text())
 if digest(args.manifest)!=frozen['input_manifest_sha256']:raise ValueError('manifest differs from frozen study')
 manifest=json.loads(args.manifest.read_text());kind=manifest['kind'];synthetic=kind=='synthetic'
 if kind not in ['synthetic','audited_history','development_cache']:raise ValueError('unsupported data kind')
 base=args.manifest.parent;frames=pinned(base,manifest['frames']);calpath=pinned(base,manifest['calendar'])
 if manifest.get('price_basis')!='raw':raise ValueError('raw-price accounting required')
 if kind=='audited_history':
  audit=json.loads(pinned(base,manifest['audit']).read_text())
  for key in ['ohlc_pass','actions_complete','calendar_verified','universe_asof_verified']:
   if audit.get(key) is not True:raise ValueError('input audit failed: '+key)
  if audit.get('frames_sha256')!=digest(frames) or audit.get('calendar_sha256')!=digest(calpath):raise ValueError('audit does not bind these inputs')
  pinned(base,manifest['corporate_action_evidence'])
 import study_engine.calendar as calendar
 rows=json.loads(calpath.read_text());days={}
 for row in rows:
  hh,mm=map(int,row['session_close_et'].split(':'));day=row['date'];datetime.fromisoformat(day)
  if day in days:raise ValueError('duplicate calendar date')
  days[day]=clock(hh,mm)
 calendar._DAYS=days;calendar._ORDERED=tuple(sorted(days));calendar.CALENDAR_HASH=digest(calpath)
 from study_engine import engine
 from learners import learn,predict
 engine.learn=lambda rows:learn(rows,args.variant)
 engine.predict=predict
 engine.SPEC['slip_bps']=args.cost_bps
 # The copied engine hard-codes 5bps labels in text; fills use SPEC.
 out.mkdir(parents=True)
 spec=json.loads((ROOT/'preregistration.json').read_text());state=engine.fresh();started=time.monotonic();count=0;marks=[];learning=[];burnin={};boundaries=[]
 def save(name,payload):(out/name).write_text(json.dumps(payload,indent=2,allow_nan=False,default=str),encoding='utf8')
 save('run_spec.json',dict(spec=spec,kind=kind,manifest_hash=digest(args.manifest),frames_hash=digest(frames),calendar_hash=digest(calpath),cost_bps=args.cost_bps,variant=args.variant,learner_code_sha256=digest(ROOT/'learners.py'),label='SYNTHETIC PLUMBING ONLY' if synthetic else 'OFFLINE HISTORICAL DEVELOPMENT; no deployment',source_hypotheses_previously_exposed=not synthetic))
 try:
  with frames.open(encoding='utf8') as stream,(out/'marks.jsonl').open('w',encoding='utf8') as ledger,(out/'events.jsonl').open('w',encoding='utf8') as events,(out/'plans.jsonl').open('w',encoding='utf8') as plans:
   for line in stream:
    if not line.strip():continue
    f=json.loads(line)
    if synthetic and any(not sym.startswith('SYNTH_') for sym in f['bars']):raise ValueError('synthetic input contains market symbol')
    f['feed_mode']='synthetic' if synthetic else 'offline_research'
    audit=f.get('action_audit') or {};audit.update(source='synthetic_declared' if synthetic else 'pinned_research_manifest',independent=not synthetic);f['action_audit']=audit
    state=engine.advance(state,f);count+=1
    for e in state['events']:events.write(json.dumps(e)+'\n')
    for book,b in state['books'].items():
     for plan in b['plans']:plans.write(json.dumps((dict(plan)|{'book':book}))+'\n')
    for m in state['marks']:ledger.write(json.dumps(m)+'\n');marks.append(m)
    if len(state['learning'])>len(learning):
     learning=state['learning'][:];save('learning.json',learning)
    complete=len(state['completed_sessions'])
    if complete==100 and not burnin:
     burnin={n:b['equity'] for n,b in state['books'].items()};burnin['through']=state['completed_sessions'][-1]
    if complete>100 and (complete-100)%30==0 and (not boundaries or boundaries[-1]['through']!=state['completed_sessions'][-1]):
     boundaries.append(dict(through=state['completed_sessions'][-1],equity={n:b['equity'] for n,b in state['books'].items()},model=state['model_id']))
     save('checkpoint.json',state);save('fold_boundaries.json',boundaries)
    if count%500==0:save('progress.json',dict(status='RUNNING',frames=count,complete_sessions=complete,model_checks=len(learning),last=state['last']))
    if time.monotonic()-started>21600:raise TimeoutError('six-hour research budget reached')
  if digest(frames)!=manifest['frames']['sha256'] or digest(calpath)!=manifest['calendar']['sha256']:raise ValueError('input changed during replay')
  metrics={}
  for name,b in state['books'].items():
   observations=[m for m in marks if m['book']==name and burnin and m['time'][:10]>burnin['through']]
   peak=burnin.get(name,b['equity']);dd=0
   for m in observations:peak=max(peak,m['equity']);dd=max(dd,1-m['equity']/peak)
   realized=sum(t['pnl'] for t in b['closed']);basis=sum(p['cost'] for p in b['positions'].values())
   residual=b['cash']+basis+b.get('receivable',0)-100000-realized-b.get('dividends',0)-b.get('external_flows',0)
   if abs(residual)>.01:raise ValueError('cash/receivable ledger failed reconciliation')
   metrics[name]=dict(ending_equity=b['equity'],cash=b['cash'],open_cost_basis=basis,realized_trade_pnl=realized,cost_dollars=b['costs'],closed_trades=len(b['closed']),ledger_residual=residual,dividends=b.get('dividends',0),receivable=b.get('receivable',0),evaluation_start_equity=burnin.get(name),evaluation_return=b['equity']/burnin[name]-1 if name in burnin else None,evaluation_max_drawdown=dd,entry_halt=b['entry_halt'])
  bret=metrics['baseline']['evaluation_return'];lret=metrics['learner']['evaluation_return']
  summary=dict(status='COMPLETED',label='SYNTHETIC ONLY — NOT MARKET EVIDENCE' if synthetic else 'historical development only',frames=count,complete_sessions=len(state['completed_sessions']),evaluation_complete_folds=len(boundaries),model_checks=len(learning),accepted_models=sum(x['status']=='accepted_prediction_model' for x in learning),books=metrics,learner_beats_baseline=None if bret is None else lret>bret,promotion=False,verdict='DEMONSTRATION_ONLY' if synthetic else 'REQUIRES_ACCOUNTING_AND_FULL_GATE_REVIEW',data_defects=list((state.get('blocked_symbols') or {}).keys()),benchmarks='not computed; unavailable does not mean zero')
  save('summary.json',summary);save('checkpoint.json',state);save('progress.json',dict(status='COMPLETED',frames=count,last=state['last']))
  print(json.dumps(summary,indent=2))
 except Exception as exc:
  save('progress.json',dict(status='BLOCKED',error=str(exc),frames=count,last=state['last']));raise
if __name__=='__main__':main()
