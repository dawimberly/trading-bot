import unittest
from copy import deepcopy
from journal_attribution import attribute,cost_buckets,journal_records

def j(**kw):
 r={'timestamp':'2026-09-01 09:00:05','event':'fill','symbol':'TEST','side':'sell','qty':'2','price':'101.25','sleeve':'NYSE','notes':'saved note','exit_reason':'fixed_stop','regime':''};r.update(kw);return r

def b(**kw):
 r={'id':'e1','activity_type':'FILL','order_id':'o1','transaction_time':'2026-09-01T14:00:00Z','symbol':'TEST','side':'sell','qty':'2','price':'101.25','leaves_qty':'0','order_status':'filled'};r.update(kw);return r

class AttributionTests(unittest.TestCase):
 def test_central_journal_clock_matches_utc_broker_and_preserves_context(self):
  out=attribute({'journal':[j()]},[b()]);self.assertEqual(out['summary']['matched_journal_fill_records'],1)
  m=out['matches'][0];self.assertEqual(m['time_delta_seconds'],5);self.assertEqual(m['journal']['exit_reason'],'fixed_stop');self.assertEqual(m['journal']['sleeve'],'NYSE');self.assertFalse(m['original_purchase_plan_reconstructed']);self.assertIsNone(out['whole_book_profit'])
 def test_one_journal_order_can_attribute_multiple_partial_executions(self):
  out=attribute({'journal':[j()]},[b(id='a',qty='1',leaves_qty='1',order_status='partially_filled',transaction_time='2026-09-01T13:59:59Z'),b(id='b',qty='1')])
  self.assertEqual(out['summary']['matched_broker_executions'],2);self.assertEqual(out['matches'][0]['match_kind'],'completed_order_group')
 def test_ambiguous_identical_orders_are_not_greedily_matched(self):
  out=attribute({'journal':[j()]},[b(),b(id='e2',order_id='o2')]);self.assertEqual(out['summary']['matched_broker_executions'],0);self.assertEqual(out['summary']['unmatched_broker_executions'],2)
 def test_two_different_journal_rows_cannot_claim_one_execution(self):
  out=attribute({'journal':[j(),j(notes='different note')]},[b()]);self.assertEqual(out['summary']['matched_broker_executions'],0)
 def test_duplicate_snapshot_observation_not_double_counted(self):
  out=attribute({'current':[j()],'backup':[j()]},[b()]);self.assertEqual(out['summary']['matched_journal_fill_records'],1);self.assertEqual(len(out['matches'][0]['provenance']),2)
 def test_identical_repeats_within_one_source_stay_ambiguous(self):
  out=attribute({'current':[j(),j()]},[b()]);self.assertEqual(out['summary']['matched_broker_executions'],0)
 def test_invalid_and_undated_broken_rows_do_not_become_fills(self):
  out=attribute({'backup':[j(timestamp='broken stack trace')]},[b()]);self.assertEqual(out['summary']['journal_fill_records'],0);self.assertEqual(out['summary']['unmatched_broker_executions'],1)
 def test_price_or_quantity_mismatch_does_not_fabricate_match(self):
  for row in [b(qty='3'),b(price='99')]:
   out=attribute({'journal':[j()]},[row]);self.assertEqual(out['summary']['matched_broker_executions'],0)
 def test_matching_window_is_not_expanded_to_find_a_trade(self):
  out=attribute({'journal':[j(timestamp='2026-09-01 09:02:01')]},[b()]);self.assertEqual(out['summary']['matched_broker_executions'],0)
 def test_execution_before_journal_start_remains_unsaved(self):
  out=attribute({'journal':[j()]},[b(transaction_time='2026-05-25T14:00:00Z')]);self.assertEqual(out['summary']['unmatched_equity_before_journal_start'],1)
 def test_nearby_signal_is_context_not_a_reconstructed_plan(self):
  rows=[j(),j(timestamp='2026-09-01 09:00:00',event='signal',regime='RHYME_D')];out=attribute({'journal':rows},[b()]);ctx=out['matches'][0]['nearby_signal_exit_context'][0];self.assertEqual(ctx['row']['regime'],'RHYME_D');self.assertEqual(ctx['link_status'],'nearby_context_not_confirmed_order_link')
 def test_conflicting_duplicate_execution_refused(self):
  with self.assertRaises(ValueError):attribute({'journal':[j()]},[b(),b(price='102')])
 def test_fees_separate_regulatory_and_crypto_without_dividend_invention(self):
  rows=[{'id':s,'activity_type':'FEE','activity_sub_type':s,'status':'executed','currency':'USD','net_amount':'-0.01'} for s in ['REG','CAT','TAF']]
  rows.append({'id':'crypto','activity_type':'CFEE','status':'executed','currency':'USD','net_amount':'-10'})
  out=cost_buckets(rows);self.assertEqual(out['buckets']['crypto_pair_transaction_fee']['USD']['net_amount'],'-10');self.assertNotIn('posted_dividend_cash',out['buckets']);self.assertFalse(out['crypto_fees_in_equity_costs']);self.assertFalse(out['dividend_strategy_added'])
 def test_positive_fee_credit_and_foreign_currency_not_absorbed_into_usd(self):
  rows=[{'id':'reg','activity_type':'FEE','activity_sub_type':'REG','status':'executed','currency':'USD','net_amount':'0.02'},{'id':'eur','activity_type':'FEE','activity_sub_type':'REG','status':'executed','currency':'EUR','net_amount':'-0.5'}]
  out=cost_buckets(rows)['buckets']['equity_regulatory_REG'];self.assertEqual(out['USD']['net_amount'],'0.02');self.assertEqual(out['EUR']['net_amount'],'-0.5')
 def test_dividends_count_only_when_posted(self):
  rows=[{'id':'pending','activity_type':'DIV','status':'pending','currency':'USD','net_amount':'3'}]
  out=cost_buckets(rows);self.assertNotIn('posted_dividend_cash',out['buckets']);self.assertEqual(len(out['unclassified']),1)
 def test_name_change_activity_is_not_sale_profit(self):
  rows=[{'id':'nc','activity_type':'NC','status':'executed','currency':'USD','net_amount':'0'}]
  out=cost_buckets(rows);self.assertIn('corporate_or_other_activity_requires_reconciliation',out['buckets']);self.assertIsNone(out['whole_book_profit'])
 def test_same_day_before_first_journal_line_is_not_before_journal_start_date(self):
  out=attribute({'journal':[j()]},[b(transaction_time='2026-09-01T10:00:00Z')])
  self.assertEqual(out['summary']['unmatched_equity_before_journal_start'],0);self.assertEqual(out['summary']['unmatched_equity_before_first_journal_timestamp'],1)
 def test_invalid_broker_clock_is_retained_without_inventing_a_zone(self):
  out=attribute({'journal':[j()]},[b(transaction_time='2026-09-01T09:00:00')])
  self.assertEqual(out['summary']['matched_broker_executions'],0);self.assertEqual(out['summary']['invalid_broker_executions'],1);self.assertEqual(out['invalid_broker_records'][0]['execution_id'],'e1')
 def test_actual_posted_dividend_record_is_cash_accounting_only(self):
  out=cost_buckets([{'id':'paid','activity_type':'DIV','status':'executed','currency':'USD','net_amount':'1.23'}])
  self.assertEqual(out['buckets']['posted_dividend_cash']['USD']['net_amount'],'1.23');self.assertFalse(out['dividend_strategy_added']);self.assertIsNone(out['whole_book_profit'])
if __name__=='__main__':unittest.main()
