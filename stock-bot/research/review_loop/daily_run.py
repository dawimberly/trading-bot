"""Private daily export/review with an allowlisted, credential-managed Git publisher."""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from datetime import datetime,timezone
from pathlib import Path
from broker_export import atomic
from review_loop import BOOKS,build,stamp
REMOTE='https://github.com/dawimberly/trading-bot.git'
BRANCH='codex/research-review-evidence-20261024'
PROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
GAPS={'stale_report_over_72h','incomplete_fill_history','unresolved_initial_inventory_or_corporate_actions','original_purchase_plans_missing','post_sale_symbols_deferred','pagination_error','conflicts','price_errors','positions_error','calendar_error','report_error','action_scoping_unverified','snapshot_errors','legacy_journal_attribution_unavailable'}

def public_summary(review,receipt):
    receipt_by={r['book']:r for r in receipt if r.get('book') in BOOKS}
    rows=[]
    for r in review['books']:
        b=r['book']
        if b not in BOOKS:raise ValueError('unexpected_book')
        def count(k):
            n=r.get(k)
            if isinstance(n,bool) or not isinstance(n,int) or n<0:raise ValueError('invalid_public_count')
            return n
        current=receipt_by.get(b,{})
        rows.append({'book':b,'source_as_of':stamp(r['as_of']).isoformat(),'purchases':count('purchases'),'sales':count('sales'),'plans_available':count('plans_available'),'post_sale_30_price_observations':count('post_sale_30_price_observations'),
            'gaps':[g if g in GAPS else 'unclassified_evidence_gap' for g in r['gaps']],
            'latest_refresh_ok':current.get('account_pin_verified') is True and 'error' not in current and current.get('report_as_of')==r['as_of'],
            'fill_pagination_complete':current.get('complete_activity_pagination') is True})
    for original,safe in zip(review['books'],rows):
        counts=original.get('legacy_decision_record_coverage') or {}
        safe['legacy_decision_record_coverage']={k:v for k,v in counts.items() if k in ('journal_fill_records','journal_signals','journal_exits','matched_journal_fill_records','matched_broker_executions','unmatched_journal_fill_records','unmatched_broker_executions','unmatched_equity_executions','unmatched_equity_before_journal_start','unmatched_equity_before_first_journal_timestamp') and isinstance(v,int) and not isinstance(v,bool) and v>=0}
    allfresh=len(rows)==3 and all(r['latest_refresh_ok'] for r in rows)
    return {'schema':1,'generated_at':stamp(review['generated_at']).isoformat(),'scope':'observational research evidence only; no deployment','target_net_annual_return':.5,'annual_target_status':'unverified','status':'EVIDENCE_GAPS' if review['status']=='EVIDENCE_GAPS' or not allfresh else 'REVIEW_ONLY',
            'fresh_export_all_books':allfresh,'books':rows,'best_strategy':None,'automatic_deployment':False,
            'notes':['No balances, positions, account IDs, order IDs, credentials, raw fills or account-return figures are published.','Price-only post-sale observations are not trade profitability or hindsight timing verdicts.','Fixed/adaptive strategy search, full portfolio accounting, missing original plans and corporate-action reconciliation remain separate unfinished work.']}

def publish(summary,folder,runner=subprocess.run):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='Never')
    def git(*args,check=True):
        return runner(['git','-c','core.hooksPath='+str(folder/'no_hooks'),'-C',str(folder),*args],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=90,check=check,creationflags=PROCESS_FLAGS)
    if not (folder/'.git').exists():
        git('init','-b',BRANCH);git('remote','add','origin',REMOTE)
    if git('remote','get-url','origin').stdout.strip()!=REMOTE:raise ValueError('publication_remote_mismatch')
    if git('branch','--show-current').stdout.strip()!=BRANCH:raise ValueError('publication_branch_mismatch')
    if set(git('ls-files').stdout.splitlines())-{'README.md','latest_review.json'}:raise ValueError('unexpected_tracked_file')
    atomic(folder/'latest_review.json',summary)
    (folder/'README.md').write_text('# Sanitized research review evidence\n\nMachine-generated allowlisted summary for issue #18 and PR #19. No broker credentials or raw account records belong on this branch. This is not deployed bot source or evidence of a profitable strategy.\n',encoding='utf8')
    git('add','--','README.md','latest_review.json')
    changed=git('diff','--cached','--quiet',check=False)
    if changed.returncode:
        git('-c','user.name=Stock-Bot Research','-c','user.email=stock-bot-research@users.noreply.github.com','commit','-m','Refresh sanitized research evidence')
    # Fixed branch only, fast-forward only. No force, merge, main checkout or repo hooks.
    git('push','origin','HEAD:refs/heads/'+BRANCH)
    return {'status':'published','branch':BRANCH,'remote':REMOTE}

def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--source-root',type=Path,required=True);a.add_argument('--private-root',type=Path,default=Path(__file__).parent/'private');a.add_argument('--publish',action='store_true');a.add_argument('--reuse-run',help='existing private run directory name, for a one-shot reviewed refresh');args=a.parse_args()
    root=Path(__file__).resolve().parent;private=args.private_root.resolve();private.mkdir(parents=True,exist_ok=True)
    if private==args.source_root.resolve() or args.source_root.resolve() in private.parents:raise ValueError('private_output_inside_production_source')
    lock=private/'daily.lock'
    try:handle=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:
        print(json.dumps({'status':'SKIPPED_LOCK_PRESENT'}));return 2
    os.close(handle)
    try:
        before={p.name for p in (private/'runs').iterdir()} if (private/'runs').exists() else set()
        if args.reuse_run:
            if Path(args.reuse_run).name!=args.reuse_run or args.reuse_run in ('.','..'):raise ValueError('invalid_run_name')
            runfolder=private/'runs'/args.reuse_run
        else:
            with (private/'daily_export.log').open('w',encoding='utf8') as log:
                subprocess.run([sys.executable,'-B',str(root/'broker_export.py'),'--source-root',str(args.source_root),'--private-root',str(private),'--fetch','--max-pages','200','--symbol-cap','80'],stdout=log,stderr=log,timeout=3900,check=False,creationflags=PROCESS_FLAGS)
            new=[p for p in (private/'runs').iterdir() if p.name not in before]
            if len(new)!=1:raise ValueError('new_run_not_unambiguous')
            runfolder=new[0]
        receipt=json.loads((runfolder/'receipt.json').read_text(encoding='utf8'))
        # Historical journal attribution is offline, separate from original purchase plans.
        try:
            with (private/'lab_attribution.log').open('w',encoding='utf8') as attrlog:
                attribution=subprocess.run([sys.executable,'-B',str(root/'refresh_lab_attribution.py'),'--source-root',str(args.source_root),'--private-root',str(private),'--run',runfolder.name,'--update-prepared-report'],stdout=attrlog,stderr=attrlog,timeout=180,check=False,creationflags=PROCESS_FLAGS)
            if attribution.returncode:raise ValueError('legacy_attribution_failed')
        except Exception:
            path=private/'reports'/'alpaca_paper.json'
            if path.exists():
                r=json.loads(path.read_text(encoding='utf8'));r['decision_record_attribution_error']='offline_legacy_attribution_failed';r.pop('decision_record_coverage',None);atomic(path,r)
        inputs={b:json.loads((private/'reports'/(b+'.json')).read_text(encoding='utf8')) for b in BOOKS if (private/'reports'/(b+'.json')).exists()}
        now=datetime.now(timezone.utc);review=build(inputs,now);review['latest_collection_run']=runfolder.name
        from review_loop import markdown
        atomic(private/'review.json',review);(private/'review.md').write_text(markdown(review),encoding='utf8')
        safe=public_summary(review,receipt);atomic(private/'sanitized_review.json',safe)
        publication={'status':'not_requested'}
        if args.publish:
            try:publication=publish(safe,private/'public_sync')
            except Exception:publication={'status':'blocked_git_publication','raw_details_not_exported':True}
        result={'status':review['status'],'fresh_export_all_books':safe['fresh_export_all_books'],'publication':publication,'broker_writes':0,'automatic_deployment':False,'completed_at':now.isoformat()}
        atomic(private/'daily_receipt.json',result);print(json.dumps(result));return int(publication['status']=='blocked_git_publication')
    finally:lock.unlink(missing_ok=True)
if __name__=='__main__':raise SystemExit(main())
