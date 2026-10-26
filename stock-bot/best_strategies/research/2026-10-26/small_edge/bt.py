import warnings; warnings.filterwarnings("ignore")
import yfinance as yf, pandas as pd, numpy as np
U=list(pd.read_csv("sp600.csv").iloc[:,0]); SEC=["XL"+s for s in "BEFIKPUVY"]
E=SEC+["SPY","TLT","GLD","SSO","BIL","IWM","VTI"]
d=yf.download(U+E,start="2006-06-01",end="2026-10-24",auto_adjust=True,progress=False,threads=True)
C=d["Close"]; V=d["Volume"]
e=C[E].dropna(); idx=e.index; C=C.reindex(idx); V=V.reindex(idx)
R=C.pct_change(fill_method=None); er=e.pct_change().fillna(0)
start=pd.Timestamp("2008-03-17"); D=idx[idx>=start]
def h1d():
    on=(e.SPY>e.SPY.rolling(200).mean()).shift(1); w=pd.Series(0.0,index=e.columns); q=1; out=[]; to=0; cur=lm=None
    for t in D:
        v=w*(1+er.loc[t]); s=v.sum()+(1-w.sum()); q*=s; w=v/s
        S=bool(on.loc[t])
        if t.month!=lm or S!=cur:
            T=pd.Series(0.0,index=e.columns); T["SSO" if S else "BIL"]=.5
            for k in SEC+["SPY","TLT","GLD"]: T[k]+=.5/12
            tr=(T-w).abs().sum(); to+=tr; q*=1-.0005*tr; w=T; cur=S; lm=t.month
        out.append(q)
    return pd.Series(out,index=D).pct_change().fillna(0), to/(len(D)/252)
def switch(p,c=.0005):
    p=p.astype(float); tr=p.diff().abs().fillna(0)*2
    r=p*er.IWM+(1-p)*er.BIL-tr*c; return r.loc[D], tr.loc[D].sum()/(len(D)/252)
pos=pd.Series(np.arange(len(idx)),index=idx); m=idx.to_period("M")
last=pd.Series(m!=np.roll(m,-1),index=idx); first_n=pos-pos.groupby(m).transform("min")
tom=last|(first_n<3)
nxt=pd.Series(idx[1:].append(pd.DatetimeIndex([idx[-1]+pd.Timedelta(days=1)])),index=idx)
gap=pd.Series(np.busday_count(idx.values.astype("datetime64[D]"),nxt.values.astype("datetime64[D]")),index=idx)
pre=gap>1
def xsec(kind,n,c=.003):
    dv=(C*V).rolling(20).median(); ok=(C>5)&(dv>2e6)
    wk=idx.to_period("W")
    reb=pd.Series(wk!=np.roll(wk,-1),index=idx) if kind=="rev" else pd.Series(m!=np.roll(m,1),index=idx)
    score=(-(C/C.shift(5)-1)) if kind=="rev" else C/C.rolling(252).max()
    W=pd.DataFrame(np.nan,index=idx,columns=U); sel=None
    for t in idx[reb.values]:
        i=pos[t]
        if i+1>=len(idx) or idx[i]<start-pd.Timedelta(days=10): continue
        s=score[U].loc[t][ok[U].loc[t]].dropna().nlargest(n).index
        row=pd.Series(0.0,index=U); row[s]=1/n; W.iloc[i+1]=row   # execute at next close
    W=W.ffill().fillna(0); held=W.shift(1).fillna(0)
    r=(held*R[U].fillna(0)).sum(1)+(1-held.sum(1))*er.BIL
    tr=W.diff().abs().sum(1).fillna(0); r=r-tr*c
    return r.loc[D], tr.loc[D].sum()/(len(D)/252)
def st(r):
    q=(1+r).cumprod(); y=len(r)/252
    return (q.iloc[-1]**(1/y)-1)*100,(q/q.cummax()-1).min()*100,r.mean()/r.std()*np.sqrt(252)
base,bto=h1d(); vti=er.VTI.loc[D]
S={"H1_TOM":switch(tom),"H2_preholiday":switch(pre),"H3_weekly_reversal":xsec("rev",20),"H4_52wk_high":xsec("hi",30)}
WIN=[("W1",None,"2012"),("W2","2013","2018"),("W3","2019","2026")]
def show(n,r,to=None):
    print(n,("turnover/yr %.1f"%to) if to is not None else "","FULL %.2f/%.1f/%.2f"%st(r))
    for w,a,b in WIN: print("   ",w,"%.2f / %.1f / %.2f"%st(r.loc[a:b]))
show("H1D_base",base,bto); show("VTI_BH",vti,0)
np.random.seed(0)
for k,(r,to) in S.items():
    show(k,r,to); ok=True
    for w,a,b in WIN:
        c,dd,sh=st(r.loc[a:b]); C1,D1,S1=st(base.loc[a:b]); C2,_,_=st(vti.loc[a:b])
        ok&= c>C1 and c>C2 and sh>=S1 and dd>=D1-1
    x=(r-base).values; bs=[np.random.choice(x,len(x)).mean()*25200 for _ in range(1000)]
    lo,hi=np.percentile(bs,[2.5,97.5]); xv=(r-vti).values.mean()*25200
    print(f"    excess vs H1D {x.mean()*25200:.2f}pp CI [{lo:.2f},{hi:.2f}] | vs VTI {xv:.2f}pp | VERDICT {'PASS' if ok and lo>0 else 'REJECT'}")
print("universe with data:",int(C[U].notna().any().sum()),"of",len(U),"; with data by 2008-03:",int(C[U].loc[:start].notna().any().sum()))
print("days in market H1:",round(tom.loc[D].mean()*100),"% H2:",round(pre.loc[D].mean()*100,1),"%")
