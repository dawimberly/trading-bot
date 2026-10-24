"""Attribute legacy Lab journal evidence to already-collected broker executions, offline."""
from __future__ import annotations
import argparse,hashlib,importlib.util,json,os,sys
from datetime import datetime,timezone
from pathlib import Path
from broker_export import atomic
from journal_attribution import attribute,cost_buckets

def read_sources(source):
    helper=source/'modules/csv_utils.py'
    spec=importlib.util.spec_from_file_location('_research_csv_utils',helper);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    # Load only the defensive CSV helper, not the production modules package/config/engine.
    folder=source/'data/portal/users/dawimberly/books/alpaca_paper'
    names=['paper_journal.csv','paper_journal.csv.bak_pre_quarantine_20260729T221617Z']
    sources={};manifest={}
    for name in names:
        path=folder/name;raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        frame=module.read_csv_file(path)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('journal_changed_during_read')
        sources[name]=frame.fillna('').to_dict(orient='records');manifest[name]={'sha256':digest,'bytes':len(raw),'csv_rows':len(frame),'reader':'local csv_utils.read_csv_file, padded records'}
    return sources,manifest

def opening_gap(snapshot,activities):
    history=snapshot.get('portfolio_history') or {};times=history.get('timestamp') or [];equity=history.get('equity') or []
    first=min((str(a.get('transaction_time') or a.get('date') or '') for a in activities if a.get('transaction_time') or a.get('date')),default=None)
    prior=[]
    if first:
        for t,v in zip(times,equity):
            try:
                day=datetime.fromtimestamp(float(t),timezone.utc).date().isoformat()
                if float(v)>0 and day<first[:10]:prior.append(day)
            except (ValueError,TypeError,OverflowError,OSError):continue
    return {'earliest_activity_date_utc_or_broker_date':first[:10] if first else None,'positive_equity_observation_dates_before_first_activity':sorted(set(prior)),'opening_cost_basis':'unknown','profit_computable':False,'note':'Daily history observations are not assumed to be verified official session closes.'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-root',type=Path,required=True);p.add_argument('--private-root',type=Path,default=Path(__file__).parent/'private');p.add_argument('--run',required=True);p.add_argument('--update-prepared-report',action='store_true');args=p.parse_args()
    source=args.source_root.resolve();private=args.private_root.resolve()
    if private==source or source in private.parents:raise ValueError('output_must_not_be_in_production_source')
    if Path(args.run).name!=args.run or args.run in ('.','..'):raise ValueError('invalid_run_name')
    sys.dontwrite_bytecode=True
    def guard(event,values):
        if event in ('socket.connect','socket.getaddrinfo'):raise ValueError('offline_attribution_network_refused')
        if event=='open':
            path,mode,flags=values
            if isinstance(path,(str,bytes,os.PathLike)) and ((isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))):
                dest=Path(path).resolve()
                if dest!=private and private not in dest.parents:raise ValueError('attribution_write_outside_private_root')
    sys.addaudithook(guard)
    run=private/'runs'/args.run;book='alpaca_paper';receipt=json.loads((run/'receipt.json').read_text(encoding='utf8'))
    if not any(r.get('book')==book and r.get('account_pin_verified') is True and 'error' not in r for r in receipt):raise ValueError('verified_lab_collection_required')
    payload=json.loads((run/book/'activities.json').read_text(encoding='utf8'));activities=payload['rows']
    sources,manifest=read_sources(source);result=attribute(sources,activities);result['generated_at']=datetime.now(timezone.utc).isoformat();result['broker_as_of']=payload['as_of'];result['activity_pagination']=payload['coverage'];result['source_files']=manifest
    result['fees_and_cash']=cost_buckets(activities);result['opening_inventory_evidence']=opening_gap(json.loads((run/book/'snapshots.json').read_text(encoding='utf8')),activities)
    atomic(run/book/'journal_attribution.json',result)
    atomic(private/'lab_journal_attribution.json',result)
    if args.update_prepared_report:
        path=private/'reports'/(book+'.json');report=json.loads(path.read_text(encoding='utf8'))
        if report.get('book')!=book or report.get('as_of')!=payload['as_of']:raise ValueError('prepared_report_not_bound_to_this_run')
        report['decision_record_coverage']=result['summary']
        report['posted_cost_classification']=result['fees_and_cash']
        report['journal_attribution_status']='legacy_evidence_matched_conservatively; not_original_plan_reconstruction'
        # Preserve existing original_plan fields and unknown inventory; attach no fabricated basis.
        atomic(path,report)
    print(json.dumps({'book':book,'source_rows':{k:v['csv_rows'] for k,v in manifest.items()},'attribution':result['summary'],'whole_book_profit_status':result['whole_book_profit_status'],'broker_requests':0}))
    return 0
if __name__=='__main__':raise SystemExit(main())
