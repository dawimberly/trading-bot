import ast,importlib.util,json,sys,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from modules import post_sale_review as r,purchase_plans as p

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.days=[]
        day=datetime(2026,8,3,20,tzinfo=timezone.utc)
        while len(self.days)<36:
            if day.weekday()<5:self.days.append(day)
            day+=timedelta(days=1)
        self.cal=[{'date':d.date().isoformat(),'close':d.isoformat()} for d in self.days]
        self.prices={'ABC':dict(basis='raw_same_units_as_broker',unit_alignment_verified=True,corporate_actions_checked_through=self.days[-1].date().isoformat(),bars=[dict(date=d.date().isoformat(),close=100+i,high=102+i,low=98+i) for i,d in enumerate(self.days)])}
        self.fills=[self.fill('b','buy',10,100,0),self.fill('s','sell',5,100,1)]
    def fill(self,oid,side,qty,price,idx,id=None):
        return dict(id=id or oid,order_id=oid,symbol='ABC',side=side,qty=str(qty),price=str(price),transaction_time=(self.days[idx]-timedelta(hours=1)).isoformat(),activity_type='FILL')
    def report(self,**kw):
        args=dict(book='alpaca_live',fills=self.fills,calendar=self.cal,prices=self.prices,as_of=self.days[-1].isoformat(),initial_inventory={});args.update(kw);return r.post_sale_report(**args)
    def test_30_trading_sessions_not_calendar_days(self):
        s=self.report()['sales'][0];self.assertEqual(s['milestones']['30']['session'],self.days[31].date().isoformat());self.assertTrue(s['complete_30_sessions'])
    def test_partial_position_not_partial_order(self):
        s=self.report()['sales'][0];self.assertEqual(s['position_effect'],'partial_position_sale');self.assertEqual(s['remaining_quantity'],5)
    def test_short_open_is_not_a_long_sale_counterfactual(self):
        self.fills=self.fills[1:]
        s=self.report()['sales'][0];self.assertEqual(s['position_effect'],'short_open_or_mixed');self.assertIsNone(s['milestones']['1']['price_return'])
    def test_full_exit(self):
        self.fills[-1]['qty']='10';self.assertEqual(self.report()['sales'][0]['position_effect'],'full_exit')
    def test_unknown_inventory_never_claims_full(self):
        self.assertEqual(self.report(initial_inventory=None)['sales'][0]['position_effect'],'unknown_inventory')
    def test_same_order_executions_aggregate_and_deduplicate(self):
        a=self.fill('s','sell',2,100,1,'s1');b=self.fill('s','sell',3,110,1,'s2');g=r.group_fills([a,b,a]);self.assertEqual(len(g),1);self.assertEqual(float(g[0]['notional']/g[0]['qty']),106)
    def test_conflicting_duplicate_rejected(self):
        a=self.fill('s','sell',2,100,1);b=dict(a,price='101')
        with self.assertRaises(ValueError):r.group_fills([a,b])
    def test_missing_exact_bar_does_not_shift_horizon(self):
        target=self.days[6].date().isoformat();self.prices['ABC']['bars']=[b for b in self.prices['ABC']['bars'] if b['date']!=target]
        s=self.report()['sales'][0];self.assertIsNone(s['milestones']['5']['price_return']);self.assertFalse(s['complete_30_sessions'])
    def test_unverified_units_block_returns(self):
        self.prices['ABC']['unit_alignment_verified']=False;self.assertIsNone(self.report()['sales'][0]['milestones']['30']['price_return'])
    def test_corporate_action_uncertainty_blocks(self):
        self.prices['ABC']['uncertain_action_dates']=[self.days[2].date().isoformat()];self.assertIsNone(self.report()['sales'][0]['milestones']['5']['price_return'])
    def test_asof_does_not_include_future_bars_or_fills(self):
        s=self.report(as_of=self.days[3].isoformat())['sales'][0];self.assertIsNone(s['milestones']['5']['session']);self.assertEqual(s['elapsed_sessions'],2)
    def test_intraday_before_calendar_close_not_complete(self):
        s=self.report(as_of=(self.days[3]-timedelta(hours=1)).isoformat())['sales'][0];self.assertEqual(s['elapsed_sessions'],1)
    def test_close_only_extrema_labeled(self):
        for b in self.prices['ABC']['bars']:b.pop('high');b.pop('low')
        self.assertEqual(self.report()['sales'][0]['extrema_basis'],'daily_close_only')
    def test_price_followthrough_not_timing_verdict(self):
        s=self.report()['sales'][0];self.assertAlmostEqual(s['milestones']['1']['price_return'],.02);self.assertIsNone(s['replacement_capital_result']);self.assertEqual(s['timing_verdict'],'not_assigned_from_hindsight')
    def test_original_plan_not_fabricated(self):
        b=self.report()['purchases'][0];self.assertEqual(b['plan_status'],'original_plan_unavailable')
    def test_plans_cannot_leak_across_books(self):
        plans=[dict(book='alpaca_paper',event='purchase_plan',plan_id='p'),dict(book='alpaca_paper',event='order_link',plan_id='p',order_id='b')]
        self.assertIsNone(self.report(plans=plans)['purchases'][0]['original_plan'])
    def test_retroactive_plan_cannot_become_original(self):
        plans=[dict(book='alpaca_live',event='purchase_plan',plan_id='p',observed_at=self.days[3].isoformat()),dict(book='alpaca_live',event='order_link',plan_id='p',order_id='b')]
        self.assertIsNone(self.report(plans=plans)['purchases'][0]['original_plan'])
    def test_book_required_and_naive_time_rejected(self):
        with self.assertRaises(ValueError):self.report(book='paper')
        with self.assertRaises(ValueError):self.report(as_of='2026-10-17T10:00:00')

class PlanTests(unittest.TestCase):
    def cfg(self):
        return SimpleNamespace(is_crypto=lambda s:False,paper_lab_concentrated_enabled=lambda:False,effective_smart_stops_enabled=lambda:False,effective_exit_optimization_enabled=lambda:False,STOP_LOSS_PCT=.05)
    def test_fixed_plan_has_no_invented_hold_or_target(self):
        rules=p.exit_rules(self.cfg());self.assertEqual(rules['loss_fraction'],.05);self.assertIsNone(rules['max_hold']);self.assertNotIn('profit_target',rules)
    def test_add_captures_cached_basis_without_fresh_calls(self):
        executor=SimpleNamespace(_positions=[SimpleNamespace(symbol='ABC',qty=2,avg_entry_price=100)])
        with patch.dict(p.os.environ,{'TRADING_BOOK_ID':'alpaca_live'}),patch.object(p,'_append') as write:
            token=p.before_submit(executor,self.cfg(),symbol='ABC',side='buy',reason='ABC/MA50',sleeve='NYSE',order=SimpleNamespace(notional=10))
        row=write.call_args.kwargs;self.assertEqual(row['role'],'add');self.assertEqual(row['before']['stop_price'],95);self.assertIsNone(row['after_fill_average']);self.assertTrue(token)
    def test_write_failure_does_not_raise(self):
        with patch.dict(p.os.environ,{'TRADING_BOOK_ID':'alpaca_live'}),patch.object(p,'_append',side_effect=OSError('secret')):
            with self.assertLogs(p._log,level='WARNING') as logs:
                self.assertIsNone(p.before_submit(SimpleNamespace(),self.cfg(),symbol='ABC',side='buy',reason='',sleeve='NYSE',order=SimpleNamespace()))
        self.assertNotIn('secret',''.join(logs.output))
    def test_sells_not_touched(self):
        with patch.object(p,'_append') as write:p.before_submit(SimpleNamespace(),self.cfg(),symbol='ABC',side='sell',reason='',sleeve='NYSE',order=SimpleNamespace())
        write.assert_not_called()
    def test_order_return_is_not_fill(self):
        with patch.object(p,'_append') as write:p.submitted(('alpaca_live','p'),SimpleNamespace(id='o',status='new'))
        self.assertFalse(write.call_args.kwargs['confirmed_fill'])
    def test_dynamic_medium_does_not_invent_stop(self):
        c=self.cfg();c.effective_smart_stops_enabled=lambda:True;c.paper_medium_strategy_enabled=lambda:True;c.ATR_STOP_MULTIPLIER=2;c.ATR_PERIOD=14;c.PAPER_POSITION_MAX_HOLD_BARS=30;c.STOP_LOSS_REEVAL_PCTS=[-.5,-.5]
        rules=p.exit_rules(c);self.assertEqual(rules['max_hold'],30);self.assertIsNone(rules['future_stop_price'])

if __name__=='__main__':unittest.main()
