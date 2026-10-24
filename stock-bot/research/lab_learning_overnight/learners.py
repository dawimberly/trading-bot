"""Frozen ranking challengers. Inputs are already-matured baseline trades only."""
import math,statistics
from datetime import datetime
from itertools import combinations
VARIANTS=('pairwise_rank','median_linear','local_expectancy')

def scale(rows):
 n=len(rows);means=[sum(r['x'][i] for r in rows)/n for i in range(3)]
 std=[max(1e-4,math.sqrt(sum((r['x'][i]-means[i])**2 for r in rows)/n)) for i in range(3)]
 return means,std

def transform(x,m,s):return [max(-5,min(5,(v-a)/b)) for v,a,b in zip(x,m,s)]

def pairs(rows):
 groups={}
 for r in rows:groups.setdefault(r['entry_session'],[]).append(r)
 return [(a,b,1 if a['y']>b['y'] else -1) for g in groups.values() for a,b in combinations(g,2) if abs(a['y']-b['y'])>1e-12]

def fit(rows,variant):
 means,std=scale(rows);xs=[transform(r['x'],means,std) for r in rows];weights=[0.,0.,0.];bias=statistics.median(r['y'] for r in rows)
 model={'kind':variant,'means':means,'scales':std,'weights':weights,'bias':bias,'n':len(rows)}
 if variant=='local_expectancy':
  model['neighbors']=[{'x':x,'y':r['y']} for x,r in zip(xs,rows)];return model
 if variant=='pairwise_rank':
  ps=pairs(rows)
  if len(ps)<20:raise ValueError('insufficient_training_pairs')
  vectors=[([a-b for a,b in zip(transform(l['x'],means,std),transform(r['x'],means,std))],y) for l,r,y in ps]
  for _ in range(300):
   grad=[0.,0.,0.]
   for diff,y in vectors:
    z=max(-40,min(40,y*sum(w*v for w,v in zip(weights,diff))));g=-y/(1+math.exp(z))
    for k in range(3):grad[k]+=g*diff[k]
   weights=[w-.02*(g/len(vectors)+.1*w) for w,g in zip(weights,grad)]
  model['weights']=weights;model['bias']=0.;return model
 for step in range(300):
  grad=[0.,0.,0.];gb=0.
  for x,r in zip(xs,rows):
   error=bias+sum(w*v for w,v in zip(weights,x))-r['y'];g=1 if error>0 else -1 if error<0 else 0;gb+=g
   for k in range(3):grad[k]+=g*x[k]
  lr=.002/(1+step/100);bias-=lr*gb/len(rows);weights=[w-lr*(g/len(rows)+.1*w) for w,g in zip(weights,grad)]
 model['weights']=weights;model['bias']=bias;return model

def predict(model,x):
 z=transform(x,model['means'],model['scales'])
 if model['kind']=='local_expectancy':
  near=sorted(model['neighbors'],key=lambda r:sum((a-b)**2 for a,b in zip(z,r['x'])))[:15]
  return sum(r['y'] for r in near)/len(near)
 return model['bias']+sum(w*v for w,v in zip(model['weights'],z))

def accuracy(ps,score):
 if not ps:return None
 total=0.
 for a,b,y in ps:
  d=score(a)-score(b);total+=.5 if abs(d)<1e-12 else int((d>0)==(y>0))
 return total/len(ps)

def learn(rows,variant):
 if variant not in VARIANTS:raise ValueError('unknown_variant')
 info={'status':'insufficient_sample','objective':variant,'samples':len(rows),'source':'matured baseline outcomes only','portfolio_promotion':False}
 days=sorted({r['exit_session'] for r in rows})
 if len(rows)<30 or len(days)<10:return None,info
 cut=days[max(1,int(len(days)*.6))-1];valid=[r for r in rows if r['exit_session']>cut]
 if not valid:return None,info
 earliest=min((r['entry_time'] for r in valid),key=lambda t:datetime.fromisoformat(t))
 train=[r for r in rows if r['exit_session']<=cut and datetime.fromisoformat(r['exit_time'])<datetime.fromisoformat(earliest)]
 info.update(cutoff=cut,train=len(train),validation=len(valid),purged_validation_entry_cutoff=earliest,train_latest_exit=max((r['exit_time'] for r in train),default=None))
 if len(train)<20 or len(valid)<10:info['status']='insufficient_purged_split';return None,info
 try:model=fit(train,variant)
 except ValueError:info['status']='insufficient_training_pairs';return None,info
 if variant=='pairwise_rank':
  ps=pairs(valid)
  if len(ps)<10:info['status']='insufficient_validation_pairs';return None,info
  metric=accuracy(ps,lambda r:predict(model,r['x']));null=accuracy(ps,lambda r:r['x'][0]);accepted=metric>null
  info.update(validation_pairs=len(ps),pair_accuracy=metric,extension_pair_accuracy=null)
 else:
  constant=statistics.median(r['y'] for r in train) if variant=='median_linear' else sum(r['y'] for r in train)/len(train)
  metric=sum(abs(predict(model,r['x'])-r['y']) for r in valid)/len(valid);null=sum(abs(constant-r['y']) for r in valid)/len(valid);accepted=metric<null
  info.update(validation_mae=metric,constant_mae=null)
 info['status']='accepted_prediction_model' if accepted else 'rejected_prediction_model'
 if not accepted:return None,info
 try:return fit(rows,variant),info
 except ValueError:info['status']='insufficient_refit_pairs';return None,info
