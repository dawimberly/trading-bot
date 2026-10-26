import warnings; warnings.filterwarnings("ignore")
import yfinance as yf, pandas as pd, numpy as np, glob
SEC=["XL"+s for s in "BEFIKPUVY"]; E=SEC+["SPY","TLT","GLD","SSO","BIL","VTI"]
e=yf.download(E,start="2006-01-01",end="2026-10-24",auto_adjust=True,progress=False)["Close"].dropna()
idx=e.index; er=e.pct_change().fillna(0)
P=[pd.read_pickle(f) for f in sorted(glob.glob("px/*.pkl"))]
C=pd.concat([p["Close"] for p in P],axis=1).reindex(idx); V=pd.concat([p["Volume"] for p in P],axis=1).reindex(idx)
C=C.loc[:,~C.columns.duplicated()]; V=V.loc[:,~V.columns.duplicated()]
R=C.pct_change(fill_method=None).clip(-0.95,5)  # guard bad ticks
dv=(C*V).rolling(20).median().shift(1); pc=C.shift(1)
ev=pd.read_csv("clusters.csv",parse_dates=["date"])
start=pd.Timestamp("2008-03-17"); end=pd.Timestamp("2026-06-30")  # last data set 2026q1 + 63d hold
D=idx[(idx>=start)&(idx<=end)]; pos=pd.Series(np.arange(len(idx)),index=idx)
trades=[]; skip={"nohist":0,"filter":0,"held":0}
held_until={}
for _,x in ev.sort_values("date").iterrows():
    if x.sym not in C.columns: skip["nohist"]+=1; continue
    i=idx.searchsorted(x.date,side="right")   # first session AFTER filing date
    if i>=len(idx): continue
    t=idx[i]
    if t<start or t>end: continue
    if held_until.get(x.sym,pd.Timestamp(0))>=t: skip["held"]+=1; continue
    if not (pc.at[t,x.sym]>2 and dv.at[t,x.sym]>1e6): skip["filter"]+=1; continue
    j=min(i+63,len(idx)-1); trades.append((x.sym,i,j)); held_until[x.sym]=idx[j]
print("clusters",len(ev),"trades",len(trades),skip)
n=len(idx); W=np.zeros((n,len(C.columns))); col={c:k for k,c in enumerate(C.columns)}
active=np.zeros((n,len(C.columns)),bool)
for s,i,j in trades: active[i+1:j+1,col[s]]=True   # bought at close i, earns i+1..j
cnt=active.sum(1); w=np.where(cnt>0,np.minimum(1/np.maximum(cnt,1),0.05),0)
W=active*w[:,None]
Rv=np.nan_to_num(R.values); r=(W*Rv).sum(1)+(1-W.sum(1))*er.BIL.values
tr=np.abs(np.diff(W,axis=0,prepend=0)).sum(1); r=pd.Series(r-tr*0.003,index=idx).loc[D]
to=tr[pos[D].values].sum()/(len(D)/252)
print("avg open positions %.1f, avg stock exposure %.0f%%"%(cnt[pos[D].values].mean(),W.sum(1)[pos[D].values].mean()*100))
# per-trade stats
tr_ret=[(C[s].iloc[j]/C[s].iloc[i]-1) for s,i,j in trades]; tr_ret=pd.Series(tr_ret).dropna()
spy_ret=pd.Series([e.SPY.iloc[j]/e.SPY.iloc[i]-1 for s,i,j in trades])
print("per-trade 63d mean %.2f%% median %.2f%% | SPY same windows mean %.2f%%"%(tr_ret.mean()*100,tr_ret.median()*100,spy_ret.mean()*100))
def h1d():
    on=(e.SPY>e.SPY.rolling(200).mean()).shift(1); w=pd.Series(0.0,index=e.columns); q=1; out=[]; cur=lm=None
    for t in D:
        v=w*(1+er.loc[t]); s=v.sum()+(1-w.sum()); q*=s; w=v/s; S=bool(on.loc[t])
        if t.month!=lm or S!=cur:
            T=pd.Series(0.0,index=e.columns); T["SSO" if S else "BIL"]=.5
            for k in SEC+["SPY","TLT","GLD"]: T[k]+=.5/12
            q*=1-.0005*(T-w).abs().sum(); w=T; cur=S; lm=t.month
        out.append(q)
    return pd.Series(out,index=D).pct_change().fillna(0)
def st(x):
    q=(1+x).cumprod(); y=len(x)/252; return (q.iloc[-1]**(1/y)-1)*100,(q/q.cummax()-1).min()*100,x.mean()/x.std()*np.sqrt(252)
base=h1d(); vti=er.VTI.loc[D]
WIN=[("W1",None,"2012"),("W2","2013","2018"),("W3","2019","2026")]
ok=True
for nme,x in [("H1D",base),("VTI",vti),("INSIDER",r)]:
    print(nme,"FULL %.2f/%.1f/%.2f"%st(x))
    for w_,a,b in WIN: print("  ",w_,"%.2f / %.1f / %.2f"%st(x.loc[a:b]))
for w_,a,b in WIN:
    c,dd,sh=st(r.loc[a:b]); C1,D1,S1=st(base.loc[a:b]); C2,_,_=st(vti.loc[a:b]); ok&= c>C1 and c>C2 and sh>=S1 and dd>=D1-1
    print("  trades entered",w_,sum(1 for s,i,j in trades if (a is None or idx[i].year>=int(a)) and idx[i].year<=int(b)))
np.random.seed(0); dx=(r-base).values; bs=[np.random.choice(dx,len(dx)).mean()*25200 for _ in range(1000)]
lo,hi=np.percentile(bs,[2.5,97.5])
print(f"turnover/yr {to:.1f}; excess vs H1D {dx.mean()*25200:.2f}pp CI [{lo:.2f},{hi:.2f}]; vs VTI {(r-vti).mean()*25200:.2f}pp; VERDICT {'PASS' if ok and lo>0 else 'REJECT'}")
