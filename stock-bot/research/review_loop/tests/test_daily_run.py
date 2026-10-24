import copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from daily_run import public_summary,publish,REMOTE,BRANCH

def review():
 return {'generated_at':'2026-10-24T01:00:00+00:00','status':'EVIDENCE_GAPS','books':[{'book':b,'as_of':'2026-10-24T00:27:53+00:00','purchases':1,'sales':1,'plans_available':1,'post_sale_30_price_observations':1,'gaps':['original_purchase_plans_missing'],'api_key':'DO_NOT_EXPORT','balance':100000,'account_id':'PRIVATE_ACCOUNT','raw_order_id':'PRIVATE_ORDER'} for b in ['alpaca_live','alpaca_paper_v2','alpaca_paper']]}
def receipts():return [{'book':r['book'],'account_pin_verified':True,'complete_activity_pagination':True,'report_as_of':r['as_of']} for r in review()['books']]
class DailyTests(unittest.TestCase):
 def test_only_allowlisted_counts_and_flags_published(self):
  data=json.dumps(public_summary(review(),receipts()))
  for private in ['DO_NOT_EXPORT','100000','PRIVATE_ACCOUNT','PRIVATE_ORDER','api_key','raw_order_id','"balance":']:self.assertNotIn(private,data)
 def test_previous_report_cannot_be_called_fresh_after_failed_refresh(self):
  rr=receipts();rr[0]={'book':'alpaca_live','error':'timeout'}
  self.assertFalse(public_summary(review(),rr)['fresh_export_all_books'])
 def test_receipt_must_bind_the_report_asof(self):
  rr=receipts();rr[0]['report_as_of']='2026-10-20T00:00:00Z'
  self.assertFalse(public_summary(review(),rr)['fresh_export_all_books'])
 def test_no_free_text_error_or_gap_can_leak_secrets(self):
  r=review();r['books'][0]['gaps']=['PRIVATE_KEY_ABC']
  self.assertNotIn('PRIVATE_KEY_ABC',json.dumps(public_summary(r,receipts())))
 def test_non_count_value_refused(self):
  r=review();r['books'][0]['purchases']='PRIVATE_KEY'
  with self.assertRaises(ValueError):public_summary(r,receipts())
 def test_git_publisher_stages_two_files_and_only_fixed_branch_no_force(self):
  calls=[]
  def run(command,**kwargs):
   calls.append(command)
   if 'get-url' in command:out=REMOTE+'\n'
   elif '--show-current' in command:out=BRANCH+'\n'
   elif 'ls-files' in command:out='README.md\nlatest_review.json\n'
   else:out=''
   return SimpleNamespace(stdout=out,returncode=1 if '--quiet' in command else 0)
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'.git').mkdir();result=publish(public_summary(review(),receipts()),p,run)
  self.assertEqual(result['status'],'published')
  push=next(c for c in calls if 'push' in c);self.assertEqual(push[-2:],['origin','HEAD:refs/heads/'+BRANCH])
  self.assertFalse(any('--force' in c or '-f' in c for c in calls))
  add=next(c for c in calls if 'add' in c);self.assertEqual(add[-3:],['--','README.md','latest_review.json'])
 def test_publication_rejects_unknown_remote(self):
  def run(command,**kwargs):return SimpleNamespace(stdout='https://evil.example/repo.git',returncode=0)
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'.git').mkdir()
   with self.assertRaises(ValueError):publish(public_summary(review(),receipts()),p,run)
 def test_legacy_decision_coverage_does_not_export_journal_notes(self):
  r=review();r['books'][2]['legacy_decision_record_coverage']={'journal_fill_records':501,'matched_journal_fill_records':165,'notes':'PRIVATE_SECRET','broker_order_id':'PRIVATE_ID'}
  out=public_summary(r,receipts());self.assertEqual(out['books'][2]['legacy_decision_record_coverage']['journal_fill_records'],501);self.assertNotIn('PRIVATE_SECRET',json.dumps(out));self.assertNotIn('PRIVATE_ID',json.dumps(out))
if __name__=='__main__':unittest.main()
