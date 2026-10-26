import warnings; warnings.filterwarnings("ignore")
import yfinance as yf, pandas as pd, os, time
s=sorted(set(pd.read_csv("clusters.csv").sym.dropna().astype(str))-{"","NAN","NONE"})
s=[x for x in s if x.replace("-","").isalnum()]
os.makedirs("px",exist_ok=True)
for i in range(0,len(s),200):
    f=f"px/b{i:05d}.pkl"
    if os.path.exists(f): continue
    d=yf.download(s[i:i+200],start="2005-12-01",end="2026-10-24",auto_adjust=True,progress=False,threads=False)
    c=d["Close"].notna().any().sum()
    if c<20 and i<6400: print("empty, rate-limited? sleeping",flush=True); time.sleep(600); continue
    d[["Close","Volume"]].to_pickle(f); print(i,len(s),c,flush=True); time.sleep(60)
