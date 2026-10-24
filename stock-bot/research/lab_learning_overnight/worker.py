"""Bounded unattended offline research supervisor. Never trades or deploys."""
import ctypes,hashlib,json,os,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent
REVIEW=Path(r'C:/workspace/stock_bot_review_loop')
PRIVATE=REVIEW/'private'
MANIFEST=Path(r'C:/workspace/lab_next_learning_study_20261023/saved_sip/manifest.json')
VARIANTS=('pairwise_rank','median_linear','local_expectancy')
BUDGET=6*3600

def now():return datetime.now(timezone.utc).isoformat()
def atomic(path,obj):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(obj,indent=2,allow_nan=False),encoding='utf8');os.replace(tmp,path)
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def memory_bytes(child):
 if os.name!='nt':return None
 class PMC(ctypes.Structure):
  _fields_=[('cb',ctypes.c_ulong),('faults',ctypes.c_ulong)]+[(n,ctypes.c_size_t) for n in ('peak_work','work','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]
 m=PMC();m.cb=ctypes.sizeof(m)
 ok=ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(int(child._handle)),ctypes.byref(m),m.cb)
 return int(m.private) if ok else None

def evaluate(folder):
 s=json.loads((folder/'summary.json').read_text());bounds=json.loads((folder/'fold_boundaries.json').read_text());state=json.loads((folder/'checkpoint.json').read_text());marks=[json.loads(l) for l in (folder/'marks.jsonl').read_text().splitlines()]
 if len(bounds)<3:raise ValueError('insufficient_folds')
 start=state['completed_sessions'][99];end=bounds[-1]['through'];mid=bounds[len(bounds)//2-1];out={}
 for name in ('baseline','learner'):
  base=s['books'][name]['evaluation_start_equity'];finish=bounds[-1]['equity'][name];middle=mid['equity'][name];peak=base;dd=0
  for m in marks:
   if m['book']==name and start<m['time'][:10]<=end:peak=max(peak,m['equity']);dd=max(dd,1-m['equity']/peak)
  out[name]={'return':finish/base-1,'max_drawdown':dd,'first_half':middle/base-1,'second_half':finish/middle-1,'all147_return':s['books'][name]['evaluation_return']}
 baseline_hash=hashlib.sha256(json.dumps([m for m in marks if m['book']=='baseline'],sort_keys=True).encode()).hexdigest()
 return {'metrics':out,'folds':len(bounds),'model_checks':s['model_checks'],'accepted_models':s['accepted_models'],'data_defects':s['data_defects'],'ledger_reconciled':all(abs(s['books'][n]['ledger_residual'])<.01 for n in out),'baseline_path_hash':baseline_hash}

def report(registry):
 results=[]
 for name in VARIANTS:
  cells={c['cost_bps']:c for c in registry['jobs'] if c['variant']==name}
  if not all(cells.get(k,{}).get('status')=='COMPLETED' for k in (5,10)):
   results.append({'variant':name,'status':'pending_or_blocked','promotion':False});continue
  a,b=cells[5]['result'],cells[10]['result'];l=a['metrics']['learner'];base=a['metrics']['baseline']
  gates={'return':l['return']>base['return'],'drawdown':l['max_drawdown']<=base['max_drawdown']+.02,'first_half':l['first_half']>base['first_half'],'second_half':l['second_half']>base['second_half'],'higher_cost':b['metrics']['learner']['return']>b['metrics']['baseline']['return'],'ledger':a['ledger_reconciled'] and b['ledger_reconciled'],'no_action_blocks':not a['data_defects'] and not b['data_defects']}
  results.append({'variant':name,'status':'development_pass_only' if all(gates.values()) else 'reject','gates':gates,'normal_cost':a['metrics'],'higher_cost':b['metrics'],'promotion':False})
 return {'batch_status':registry['status'],'updated_at':registry['updated_at'],'completed_jobs':sum(j['status']=='COMPLETED' for j in registry['jobs']),'total_jobs':len(registry['jobs']),'candidates':results,'lab_better_than_live_and_medium':'NOT_PROVEN','promotion':False,'limitations':['previously exposed247session29namepanel','required protocol windows and faithful Live/Medium decision inputs unavailable','no paid data or delayed-feed execution; risk and live books unchanged']}

def publish(registry):
 public=report(registry);atomic(ROOT/'REPORT.json',public)
 lines=['# Overnight Lab research','',f"Status: {public['batch_status']}; {public['completed_jobs']}/{public['total_jobs']} jobs complete.",'','Lab superiority over Live and Medium: NOT PROVEN. No deployment.','', 'All candidates, including failures:','']
 for c in public['candidates']:lines.append('- '+c['variant']+': '+c['status'])
 (ROOT/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
 lock=PRIVATE/'daily.lock'
 try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd)
 except FileExistsError:return 'publication_deferred_daily_review_running'
 try:
  sys.path.insert(0,str(REVIEW));from daily_run import publish as git_publish
  safe=json.loads((PRIVATE/'sanitized_review.json').read_text(encoding='utf8'));safe['overnight_research']=public
  result=git_publish(safe,PRIVATE/'public_sync');return result['status']
 except Exception:return 'publication_blocked; local results preserved'
 finally:lock.unlink(missing_ok=True)

def main():
 lock=ROOT/'WORKER_LOCK'
 try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.write(fd,str(os.getpid()).encode());os.close(fd)
 except FileExistsError:raise SystemExit('worker lock exists; inspect before any restart')
 started=time.monotonic();child=None;registry={'status':'STARTING','pid':os.getpid(),'started_at':now(),'updated_at':now(),'jobs':[{'variant':v,'cost_bps':c,'status':'QUEUED'} for v in VARIANTS for c in (5,10)],'auto_deploy':False,'baseline_paths':{}}
 flags=(subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name=='nt' else 0
 try:
  pins=json.loads((ROOT/'SOURCE_HASHES.json').read_text())
  for name,value in pins.items():
   if sha(ROOT/name)!=value:raise ValueError('source_hash_changed_before_run')
  if os.name=='nt':ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
  registry['status']='RUNNING';registry['publication']=publish(registry);atomic(ROOT/'registry.json',registry)
  for cell in registry['jobs']:
   if time.monotonic()-started>BUDGET:registry['status']='BUDGET_REACHED';break
   name=cell['variant']+'_'+str(cell['cost_bps'])+'_r2';cell.update(status='RUNNING',started_at=now())
   with (ROOT/(name+'.out.log')).open('w') as out,(ROOT/(name+'.err.log')).open('w') as err:
    childenv=dict(os.environ,PYTHONPATH=os.pathsep.join(p for p in sys.path if 'site-packages' in p))
    child=subprocess.Popen([sys._base_executable,'-B',str(ROOT/'run_variant.py'),'--manifest',str(MANIFEST),'--output',name,'--cost-bps',str(cell['cost_bps']),'--variant',cell['variant']],cwd=ROOT,stdout=out,stderr=err,creationflags=flags,env=childenv)
    cell['pid']=child.pid;jobstart=time.monotonic();reason=None
    while child.poll() is None:
     used=memory_bytes(child)
     if used is None and os.name=='nt' and child.poll() is None:reason='memory_monitor_unavailable'
     if used and used>2*1024**3:reason='memory_budget'
     if time.monotonic()-jobstart>2700:reason='job_time_budget'
     if time.monotonic()-started>BUDGET:reason='batch_time_budget'
     if reason:
      child.terminate();child.wait(timeout=20);break
     cell['private_memory_bytes']=used;registry['updated_at']=now();atomic(ROOT/'registry.json',registry);time.sleep(10)
    exitcode=child.returncode;child=None
   cell['exit_code']=exitcode;cell['finished_at']=now()
   try:
    if reason or exitcode!=0:raise ValueError(reason or 'replay_failed')
    result=evaluate(ROOT/'results'/name);cell['result']=result
    old=registry['baseline_paths'].get(str(cell['cost_bps']))
    if old and old!=result['baseline_path_hash']:raise ValueError('baseline_path_mismatch')
    registry['baseline_paths'][str(cell['cost_bps'])]=result['baseline_path_hash'];cell['status']='COMPLETED'
    if not result['ledger_reconciled'] or result['data_defects']:raise ValueError('data_or_accounting_block')
   except Exception as exc:cell['status']='BLOCKED';cell['error']=str(exc)[:200]
   registry['updated_at']=now();registry['publication']=publish(registry);atomic(ROOT/'registry.json',registry)
   if cell.get('error') in ('data_or_accounting_block','baseline_path_mismatch'):registry['status']='BLOCKED_DATA_OR_ACCOUNTING';break
  else:registry['status']='COMPLETED_FROZEN_QUEUE'
  registry['updated_at']=now();registry['publication']=publish(registry);atomic(ROOT/'registry.json',registry)
 except Exception as exc:
  registry.update(status='WORKER_BLOCKED',error=type(exc).__name__,updated_at=now());atomic(ROOT/'registry.json',registry)
  try:publish(registry)
  except Exception:pass
  raise
 finally:
  if child is not None and child.poll() is None:child.terminate()
  if os.name=='nt':ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
  lock.unlink(missing_ok=True)
if __name__=='__main__':main()
