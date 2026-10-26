import importlib.util
import unittest
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
spec=importlib.util.spec_from_file_location('accounting',Path(__file__).resolve().parents[1]/'modules/dashboard_accounting.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
NOW=datetime(2026,10,16,16,0,tzinfo=a.ET)
class Broker:
 def __init__(self,rows=None,fail=False): self.rows=rows or []; self.fail=fail; self.calls=[]
 def get(self,path,data):
  self.calls.append(path)
  if self.fail: raise RuntimeError('private broker detail')
  if path=='/calendar': return [{'date':'2026-10-15','close':'16:00'},{'date':'2026-10-16','close':'16:00'}]
  if path=='/account/activities': return self.rows
  raise AssertionError(path)
class AccountingTests(unittest.TestCase):
 def report(self,book,eq,prev,broker=None):
  return a.account_report(broker or Broker(),SimpleNamespace(equity=eq,last_equity=prev),username='dawimberly',book_id=book,now=NOW)
 def test_all_three_verified_books(self):
  for book,eq,prev,pnl,day in [('alpaca_live',294.56,300.71,-4.94,-6.15),('alpaca_paper_v2',97642.38,100381.43,-2357.62,-2739.05),('alpaca_paper',94626.51,95144.21,-3599.95,-517.70)]:
   with self.subTest(book=book):
    r=self.report(book,eq,prev);self.assertIsNone(r['reason']);self.assertAlmostEqual(r['investment_pnl'],pnl);self.assertAlmostEqual(r['daily_pnl'],day)
 def test_deposit_not_profit(self):
  b=Broker([{'id':'1','activity_type':'CSD','status':'executed','transaction_time':'2026-10-16T19:40:00Z','net_amount':'20'}])
  r=self.report('alpaca_live',314.56,300.71,b);self.assertAlmostEqual(r['investment_pnl'],-4.94);self.assertAlmostEqual(r['daily_pnl'],-6.15)
 def test_withdrawal_not_loss(self):
  b=Broker([{'id':'1','activity_type':'CSW','status':'executed','transaction_time':'2026-10-16T19:40:00Z','net_amount':'-20'}])
  r=self.report('alpaca_live',274.56,300.71,b);self.assertAlmostEqual(r['daily_pnl'],-6.15)
 def test_zero_is_not_missing(self):
  r=self.report('alpaca_live',300.71,300.71);self.assertEqual(r['daily_pnl'],0);self.assertEqual(r['daily_pct'],0)
 def test_unknown_transfer_blocks_metrics(self):
  for kind in ['JNLC','JNLS','ACATC','ACATS','TRANS']:
   with self.subTest(kind=kind):
    r=self.report('alpaca_live',294.56,300.71,Broker([{'id':'x','activity_type':kind}]));self.assertIsNone(r['daily_pnl']);self.assertIsNone(r['investment_pnl'])
 def test_api_failure_no_secret_or_false_zero(self):
  r=self.report('alpaca_live',294.56,300.71,Broker(fail=True));self.assertIsNone(r['daily_pnl']);self.assertNotIn('private',str(r))
 def test_other_profile_no_shared_anchor(self):
  r=a.account_report(Broker(),SimpleNamespace(equity=1,last_equity=1),username='other',book_id='alpaca_paper_v2',now=NOW);self.assertIsNone(r['investment_pnl'])
 def test_date_only_on_boundary_is_unknown(self):
  r=self.report('alpaca_live',294.56,300.71,Broker([{'id':'x','activity_type':'CSD','status':'executed','date':'2026-10-16','net_amount':'20'}]));self.assertIsNone(r['daily_pnl'])
 def test_daily_percent_uses_prior_close(self):
  r=self.report('alpaca_live',90,100);self.assertEqual(r['daily_pct'],-10)
 def test_signed_withdrawal_required(self):
  r=self.report('alpaca_live',294.56,300.71,Broker([{'id':'x','activity_type':'CSW','status':'executed','transaction_time':'2026-10-16T19:40:00Z','net_amount':'20'}]));self.assertIsNone(r['daily_pnl'])
 def test_missing_activity_id_is_unknown(self):
  r=self.report('alpaca_live',294.56,300.71,Broker([{'activity_type':'CSD'}]));self.assertIsNone(r['daily_pnl'])
 def test_read_only_calls(self):
  b=Broker();self.report('alpaca_live',294.56,300.71,b);self.assertEqual(b.calls,['/calendar','/account/activities'])
if __name__=='__main__': unittest.main()
