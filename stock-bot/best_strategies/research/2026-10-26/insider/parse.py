import zipfile,glob,pandas as pd
out=[]
for f in sorted(glob.glob("raw/*.zip")):
    z=zipfile.ZipFile(f); rd=lambda n,c: pd.read_csv(z.open(n),sep="\t",usecols=c,dtype=str,on_bad_lines="skip",quoting=3)
    s=rd("SUBMISSION.tsv",["ACCESSION_NUMBER","FILING_DATE","DOCUMENT_TYPE","ISSUERCIK","ISSUERTRADINGSYMBOL"])
    s=s[s.DOCUMENT_TYPE.isin(["4","4/A"])]
    t=rd("NONDERIV_TRANS.tsv",["ACCESSION_NUMBER","TRANS_CODE","TRANS_ACQUIRED_DISP_CD","TRANS_SHARES","TRANS_PRICEPERSHARE"])
    t=t[(t.TRANS_CODE=="P")&(t.TRANS_ACQUIRED_DISP_CD=="A")]
    o=rd("REPORTINGOWNER.tsv",["ACCESSION_NUMBER","RPTOWNERCIK"]).drop_duplicates("ACCESSION_NUMBER")
    t["V"]=pd.to_numeric(t.TRANS_SHARES,errors="coerce")*pd.to_numeric(t.TRANS_PRICEPERSHARE,errors="coerce")
    t=t.groupby("ACCESSION_NUMBER").V.sum().reset_index()
    m=t.merge(s,on="ACCESSION_NUMBER").merge(o,on="ACCESSION_NUMBER"); out.append(m); print(f,len(m),flush=True)
d=pd.concat(out); d["FILING_DATE"]=pd.to_datetime(d.FILING_DATE,format="mixed",dayfirst=False,errors="coerce")
d.to_parquet("purchases.parquet") if False else d.to_csv("purchases.csv",index=False); print(len(d), d.FILING_DATE.min(), d.FILING_DATE.max())
