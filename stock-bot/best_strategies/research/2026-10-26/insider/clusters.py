import pandas as pd
d=pd.read_csv("purchases.csv",parse_dates=["FILING_DATE"]).dropna(subset=["FILING_DATE","V"])
d=d[d.V>0]; ev=[]
for cik,g in d.groupby("ISSUERCIK"):
    g=g.sort_values("FILING_DATE"); prev=0
    for day in g.FILING_DATE.unique():
        w=g[(g.FILING_DATE<=day)&(g.FILING_DATE>day-pd.Timedelta(days=30))]
        n=(w.groupby("RPTOWNERCIK").V.sum()>=1e4).sum()
        if n>=3 and prev<3:
            sym=str(g[g.FILING_DATE==day].ISSUERTRADINGSYMBOL.dropna().iloc[-1] if g[g.FILING_DATE==day].ISSUERTRADINGSYMBOL.notna().any() else "")
            ev.append((cik,day,sym.upper().strip().replace(".","-")))
        prev=n
e=pd.DataFrame(ev,columns=["cik","date","sym"]); e.to_csv("clusters.csv",index=False)
print(len(e),"clusters;",e.sym.nunique(),"symbols"); print(e.date.dt.year.value_counts().sort_index().to_string())
