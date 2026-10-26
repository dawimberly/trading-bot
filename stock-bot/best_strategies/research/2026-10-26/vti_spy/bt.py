import yfinance as yf, pandas as pd, numpy as np
T=["SPY","VTI","SSO","BIL","TLT","GLD"]+["XL"+s for s in "BEFIKPUVY"]
px=yf.download(T,start="2006-01-01",end="2026-10-24",auto_adjust=True,progress=False)["Close"].dropna()
op=None
ret=px.pct_change().fillna(0)
COST=0.0005  # 5bp/side
def run(sigsym="SPY",bcore="SPY",dyn=False):
    sma=px[sigsym].rolling(200).mean(); on=(px[sigsym]>sma).shift(1)  # signal prior close, trade next day
    mom=px["VTI"]/px["VTI"].shift(126)-px["SPY"]/px["SPY"].shift(126)
    w_prev=pd.Series(0.0,index=px.columns); eq=[1.0]; to=0; cur=None; core=bcore; last_m=None
    dates=px.index[201:]
    for i,d in enumerate(dates):
        r=ret.loc[d]; 
        # drift weights
        val=(w_prev*(1+r)); g=val.sum() if val.sum()>0 else 1
        port=eq[-1]*( (w_prev*(1+r)).sum() + (1-w_prev.sum()) )
        w=val/g*(w_prev.sum()) if w_prev.sum()>0 else w_prev
        s=bool(on.loc[d]); m=d.month
        if dyn and m!=last_m: core="VTI" if mom.shift(1).loc[d]>0 else "SPY"
        if m!=last_m or s!=cur:
            t=pd.Series(0.0,index=px.columns)
            t["SSO" if s else "BIL"]+=0.5
            for x in ["XL"+c for c in "BEFIKPUVY"]+[core,"TLT","GLD"]: t[x]+=0.5/12
            tr=(t-w).abs().sum(); to+=tr; port*=1-COST*tr; w=t; cur=s; last_m=m
        w_prev=w; eq.append(port)
    e=pd.Series(eq[1:],index=dates); return e,to/(len(dates)/252)
def stats(e):
    r=e.pct_change().dropna(); y=len(r)/252
    return dict(CAGR=(e.iloc[-1]/e.iloc[0])**(1/y)-1,MaxDD=(e/e.cummax()-1).min(),Sharpe=r.mean()/r.std()*np.sqrt(252))
V={"a_baseline":run(),"b_VTI_in_B":run(bcore="VTI"),"c_VTI_signal":run(sigsym="VTI"),"d_dynamic":run(dyn=True)}
W=[("FULL",None,None),("W1 2007-2012","2007","2012"),("W2 2013-2018","2013","2018"),("W3 2019-2026","2019","2026")]
base=V["a_baseline"][0].pct_change()
for k,(e,to) in V.items():
    print(k,f"turnover/yr={to:.2f}")
    for n,a,b in W:
        s=stats(e.loc[a:b] if a else e); print(f"  {n}: CAGR {s['CAGR']*100:.2f}% DD {s['MaxDD']*100:.1f}% Sharpe {s['Sharpe']:.2f}")
    if k!="a_baseline":
        d=(e.pct_change()-base).dropna(); bs=[np.random.choice(d.values,len(d)).mean()*252 for _ in range(1000)]
        print(f"  ann diff vs base {d.mean()*252*100:.2f}pp, 95% CI [{np.percentile(bs,2.5)*100:.2f},{np.percentile(bs,97.5)*100:.2f}]pp")
print("first date",V["a_baseline"][0].index[0].date())
r=px[["SPY","VTI"]].pct_change().dropna(); print("SPY/VTI daily corr",round(r.corr().iloc[0,1],4))
on1=(px.SPY>px.SPY.rolling(200).mean()); on2=(px.VTI>px.VTI.rolling(200).mean()); print("signal agree %",round((on1==on2)[200:].mean()*100,1), "flips SPY",int(on1[200:].diff().abs().sum()),"VTI",int(on2[200:].diff().abs().sum()))
