import warnings; warnings.filterwarnings("ignore")
import yfinance as yf, pandas as pd, numpy as np, io, urllib.request
SEC=["XL"+s for s in "BEFIKPUVY"]; B=SEC+["SPY","TLT","GLD"]
T=B+["SSO","BIL","HYG","IEF"]
px=yf.download(T,start="2006-01-01",end="2026-10-24",auto_adjust=True,progress=False)["Close"].dropna()
def fred(s):
    d=pd.read_csv(io.StringIO(urllib.request.urlopen(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={s}").read().decode()),index_col=0,parse_dates=True)
    return pd.to_numeric(d.iloc[:,0],errors="coerce").reindex(px.index).ffill().shift(1)  # +1 day lag
ret=px.pct_change().fillna(0)
sig={}
c=ret["SPY"].rolling(63).corr(ret["TLT"]); sig["H1"]=c>0
ry=fred("DFII10"); sig["H2"]=(ry-ry.shift(63))<0
r=px.HYG/px.IEF; sig["H3"]=r<r.rolling(126).mean()
dx=fred("DTWEXBGS"); sig["H4"]=(dx-dx.shift(126))>0
sig={k:v.shift(1) for k,v in sig.items()}   # known at prior close
w0=0.5/12
def sleeveB(h,on):
    w=pd.Series(w0,index=B); x=pd.Series(0.0,index=["BIL"])
    if h and on:
        if h=="H1": x["BIL"]+=w["TLT"]; w["TLT"]=0
        if h=="H2":
            o=[b for b in B if b!="GLD"]; w["GLD"]=2*w0; w[o]=w[o]*(0.5-2*w0)/w[o].sum()
        if h=="H3":
            eq=SEC+["SPY"]; f=w[eq].sum()/2; w[eq]/=2; w["TLT"]+=f/2; x["BIL"]+=f/2
        if h=="H4":
            cut=["XLE","XLB","GLD"]; f=w[cut].sum()/2; w[cut]/=2; o=[b for b in B if b not in cut]; w[o]+=f/len(o)
    return pd.concat([w,x]).groupby(level=0).sum()
on=(px.SPY>px.SPY.rolling(200).mean()).shift(1)
start=max(px.index[201], dx.first_valid_index()+pd.Timedelta(days=200), ry.first_valid_index())
dates=px.index[px.index>=start]
def run(h=None):
    w=pd.Series(0.0,index=px.columns); eq=1.0; out=[]; to=0; cur=None; lm=None; tb=None
    for d in dates:
        r=ret.loc[d]; v=w*(1+r); eq*=v.sum()+(1-w.sum()); w=v/(v.sum()+(1-w.sum()))
        s=bool(on.loc[d]); m=d.month
        if m!=lm: tb=sleeveB(h, bool(sig[h].loc[d]) if h else False)
        if m!=lm or s!=cur:
            t=pd.Series(0.0,index=px.columns); t["SSO" if s else "BIL"]+=0.5
            for k,val in tb.items(): t[k]+=val
            tr=(t-w).abs().sum(); to+=tr; eq*=1-0.0005*tr; w=t; cur=s; lm=m
        out.append(eq)
    return pd.Series(out,index=dates), to/(len(dates)/252)
def st(e):
    r=e.pct_change().dropna(); y=len(r)/252
    return ((e.iloc[-1]/e.iloc[0])**(1/y)-1)*100,(e/e.cummax()-1).min()*100,r.mean()/r.std()*np.sqrt(252)
W=[("W1",None,"2012"),("W2","2013","2018"),("W3","2019","2026")]
be,bto=run(); np.random.seed(0)
print("start",dates[0].date(),"end",dates[-1].date())
def show(n,e,to):
    print(f"{n} turnover/yr {to:.2f}  FULL CAGR/DD/Sh %.2f/%.1f/%.2f"%st(e))
    for wn,a,b in W: print(f"   {wn}: %.2f / %.1f / %.2f"%st(e.loc[a:b]))
show("BASE",be,bto)
for h in ["H1","H2","H3","H4"]:
    e,to=run(h); show(h,e,to)
    print("   % months tilted:",round(sig[h].loc[dates].mean()*100))
    ok=True
    for wn,a,b in W:
        c,dd,sh=st(e.loc[a:b]); C,DD,SH=st(be.loc[a:b]); ok&= c>C and sh>=SH and dd>=DD-1.0
    d=(e.pct_change()-be.pct_change()).dropna().values
    bs=[np.random.choice(d,len(d)).mean()*252*100 for _ in range(1000)]
    lo,hi=np.percentile(bs,[2.5,97.5]); print(f"   excess {d.mean()*25200:.2f}pp/yr CI [{lo:.2f},{hi:.2f}]  VERDICT:",
      "PASS" if ok and lo>0 else "REJECT")
