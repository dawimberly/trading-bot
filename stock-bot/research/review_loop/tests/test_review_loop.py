import copy,json,unittest
from datetime import datetime,timezone
from review_loop import annual_target,review_book,review_experiment,build
NOW=datetime(2026,10,24,tzinfo=timezone.utc)
def report():
 return {'schema':1,'book':'alpaca_live','as_of':'2026-10-23T21:00:00+00:00','coverage':{'complete_from_start':True,'truncated':False,'initial_inventory':'verified'},'purchases':[{'original_plan':{'stop':.05}}],'sales':[]}
class ReviewTests(unittest.TestCase):
 def test_short_period_cannot_claim_50_percent_annual_target(self):
  self.assertEqual(annual_target(.5,120,True)['status'],'unverified')
 def test_deposit_contamination_cannot_pass_target(self):
  self.assertEqual(annual_target(1,252,False)['status'],'unverified')
 def test_actual_annual_target_and_multiyear_compounding(self):
  self.assertEqual(annual_target(.5,252,True)['status'],'met_historically')
  self.assertAlmostEqual(annual_target(1.25,504,True)['cagr'],.5)
 def test_invalid_returns_and_sample_counts(self):
  for ret,n in [(float('nan'),252),(True,252),(.8,True),(-1.1,252)]:self.assertEqual(annual_target(ret,n,True)['status'],'unverified')
 def test_cross_book_and_naive_time_are_refused(self):
  r=report()
  with self.assertRaises(ValueError):review_book(r,'alpaca_paper',NOW)
  r['as_of']='2026-10-23T12:00:00'
  with self.assertRaises(ValueError):review_book(r,'alpaca_live',NOW)
 def test_future_report_is_not_current_evidence(self):
  r=report();r['as_of']='2026-10-25T00:00:00+00:00'
  with self.assertRaises(ValueError):review_book(r,'alpaca_live',NOW)
 def test_unknown_inventory_is_not_a_completed_roundtrip(self):
  r=report();r['coverage']['initial_inventory']='unknown';r['sales']=[{'position_effect':'unknown_inventory'}]
  out=review_book(r,'alpaca_live',NOW)
  self.assertEqual(out['verified_full_exits'],0);self.assertIn('unresolved_initial_inventory_or_corporate_actions',out['gaps'])
 def test_mature_does_not_mean_missing_price_equals_zero(self):
  r=report();r['sales']=[{'complete_30_sessions':True,'elapsed_sessions':30,'milestones':{'30':{'session':'2026-10-20','price_return':None}}}]
  out=review_book(r,'alpaca_live',NOW);self.assertEqual(out['post_sale_30_price_observations'],0);self.assertIsNone(out['post_sale_30_mean_price_return'])
 def test_immature_and_future_followthrough_excluded(self):
  r=report();r['sales']=[{'complete_30_sessions':True,'elapsed_sessions':30,'milestones':{'30':{'session':'2026-11-20','price_return':.9}}},{'complete_30_sessions':False,'elapsed_sessions':10,'milestones':{'30':{'session':'2026-10-20','price_return':.5}}}]
  self.assertEqual(review_book(r,'alpaca_live',NOW)['post_sale_30_price_observations'],0)
 def test_cost_stress_tie_rejects_even_when_nominal_model_wins(self):
  gates={k:True for k in ['three_folds','net_return','drawdown','first_half','second_half','higher_cost','no_engine_action_blocks']};gates['higher_cost']=False
  e=review_experiment({'label':'PREVIOUSLY EXPOSED DEVELOPMENT DIAGNOSTIC','gates':gates})
  self.assertEqual(e['status'],'reject');self.assertFalse(e['promotion'])
 def test_missing_gate_or_development_pass_never_promotes(self):
  x=review_experiment({'label':'PREVIOUSLY EXPOSED DEVELOPMENT DIAGNOSTIC','gates':{}})
  self.assertEqual(x['status'],'reject');self.assertFalse(x['promotion'])
 def test_sensitive_source_fields_are_not_republished(self):
  r=report();r['coverage'].update(account_last4='1234',account_sha256='private',api_key='NEVER_PUBLISH');r['purchases'][0]['order_id']='secret_order'
  raw=json.dumps(review_book(r,'alpaca_live',NOW))
  for s in ['1234','private','NEVER_PUBLISH','secret_order']:self.assertNotIn(s,raw)
 def test_missing_book_and_stale_history_do_not_choose_winner(self):
  r=report();r['as_of']='2026-10-10T00:00:00Z';out=build({'alpaca_live':r},NOW)
  self.assertEqual(out['status'],'EVIDENCE_GAPS');self.assertEqual(len(out['errors']),2);self.assertIsNone(out['best_strategy']);self.assertFalse(out['automatic_deployment'])
 def test_historical_journal_evidence_is_distinct_from_original_plans(self):
  r=report();r['purchases'][0]['original_plan']=None;r['decision_record_coverage']={'journal_fill_records':501,'matched_journal_fill_records':165,'notes':'do not expose'}
  out=review_book(r,'alpaca_live',NOW);self.assertEqual(out['plans_available'],0);self.assertEqual(out['legacy_decision_record_coverage']['journal_fill_records'],501);self.assertNotIn('notes',out['legacy_decision_record_coverage'])
if __name__=='__main__':unittest.main()
