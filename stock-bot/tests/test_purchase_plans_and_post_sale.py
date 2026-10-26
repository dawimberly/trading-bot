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
        self.prices={'ABC':dict(basis='raw_same_units_as_broker',unit_alignment_verified=True,corporate_actions_checked_from=self.days[0].date().isoformat(),corporate_actions_checked_through=self.days[-1].date().isoformat(),position_changing_action_dates=[],bars=[dict(date=d.date().isoformat(),close=100+i,high=102+i,low=98+i) for i,d in enumerate(self.days)])}
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

    def linked_plan(self):
        return [dict(book='alpaca_live',event='purchase_plan',plan_id='p',symbol='ABC',side='buy',observed_at=(self.days[0]-timedelta(hours=2)).isoformat(),exit_rules=dict(kind='fixed_entry_stop',loss_fraction=.05)),dict(book='alpaca_live',event='order_link',plan_id='p',order_id='b',observed_at=(self.days[0]-timedelta(minutes=59)).isoformat())]
    def snapshot(self,**kw):
        row=dict(book='alpaca_live',after_order_id='b',symbol='ABC',source='alpaca_positions',observed_at=(self.days[0]-timedelta(minutes=58)).isoformat(),qty='10',avg_entry_price='100');row.update(kw);return row
    def test_verified_fill_links_original_plan_and_stop_reference(self):
        b=self.report(plans=self.linked_plan(),position_snapshots=[self.snapshot()])['purchases'][0]
        self.assertEqual(b['plan_status'],'contemporaneous_plan_linked');self.assertEqual(b['filled_plan']['broker_average_entry_after'],100);self.assertEqual(b['filled_plan']['rule_implied_stop_after_fill'],95);self.assertFalse(b['filled_plan']['stop_is_broker_order'])
    def test_add_uses_broker_after_average_not_old_cached_basis(self):
        add=self.fill('a','buy',5,110,0);add['transaction_time']=(self.days[0]-timedelta(minutes=55)).isoformat();self.fills.insert(1,add)
        plans=self.linked_plan();plans+=[dict(plans[0],plan_id='add',observed_at=(self.days[0]-timedelta(minutes=56)).isoformat()),dict(plans[1],plan_id='add',order_id='a',observed_at=(self.days[0]-timedelta(minutes=54)).isoformat())]
        snap=self.snapshot(after_order_id='a',qty='15',avg_entry_price='103.333333',observed_at=(self.days[0]-timedelta(minutes=53)).isoformat())
        b=self.report(plans=plans,position_snapshots=[snap])['purchases'][1]
        self.assertEqual(b['position_role'],'add');self.assertAlmostEqual(b['filled_plan']['rule_implied_stop_after_fill'],103.333333*.95)
    def test_missing_snapshot_does_not_invent_position_average(self):
        b=self.report(plans=self.linked_plan())['purchases'][0];self.assertIsNone(b['filled_plan']['broker_average_entry_after'])
    def test_wrong_book_and_future_snapshots_not_used(self):
        b=self.report(plans=self.linked_plan(),position_snapshots=[self.snapshot(book='alpaca_paper'),self.snapshot(observed_at=(self.days[-1]+timedelta(days=1)).isoformat())])['purchases'][0]
        self.assertIsNone(b['filled_plan']['broker_average_entry_after'])
    def test_snapshot_after_other_trade_cannot_explain_original_buy(self):
        b=self.report(plans=self.linked_plan(),position_snapshots=[self.snapshot(observed_at=self.days[2].isoformat())])['purchases'][0]
        self.assertIsNone(b['filled_plan']['broker_average_entry_after'])
    def test_conflicting_snapshots_are_unavailable(self):
        b=self.report(plans=self.linked_plan(),position_snapshots=[self.snapshot(),self.snapshot(avg_entry_price='110')])['purchases'][0]
        self.assertEqual(b['filled_plan']['snapshot_status'],'conflicting_post_fill_snapshots')
    def test_conflicting_plans_not_last_row_wins(self):
        plans=self.linked_plan();plans.append(dict(plans[0],symbol='XYZ'))
        self.assertIsNone(self.report(plans=plans)['purchases'][0]['original_plan'])
    def test_conflicting_order_links_not_last_row_wins(self):
        plans=self.linked_plan();plans.append(dict(plans[1],plan_id='other'))
        self.assertIsNone(self.report(plans=plans)['purchases'][0]['original_plan'])
    def test_wrong_symbol_plan_not_used(self):
        plans=self.linked_plan();plans[0]['symbol']='XYZ'
        self.assertIsNone(self.report(plans=plans)['purchases'][0]['original_plan'])
    def test_calendar_must_cover_sale_session(self):
        s=self.report(calendar=self.cal[3:])['sales'][0];self.assertIn('calendar_missing_sale_session',s['milestones']['1']['unavailable_reasons']);self.assertIsNone(s['milestones']['1']['session'])
    def test_calendar_close_date_must_agree(self):
        self.cal[1]['date']=self.cal[0]['date']
        with self.assertRaises(ValueError):self.report()
    def test_duplicate_price_dates_rejected(self):
        self.prices['ABC']['bars'].append(self.prices['ABC']['bars'][0])
        with self.assertRaises(ValueError):self.report()
    def test_action_coverage_requires_start_and_end(self):
        self.prices['ABC'].pop('corporate_actions_checked_from')
        s=self.report()['sales'][0];self.assertIsNone(s['milestones']['1']['price_return'])
    def test_split_cannot_look_like_a_price_loss(self):
        self.prices['ABC']['position_changing_action_dates']=[self.days[2].date().isoformat()]
        s=self.report()['sales'][0];self.assertIn('position_changing_corporate_action',s['milestones']['1']['unavailable_reasons']);self.assertFalse(s['complete_30_sessions'])
    def test_nonfinite_initial_inventory_rejected(self):
        with self.assertRaises(ValueError):self.report(initial_inventory={'ABC':'NaN'})
    def test_markdown_reports_uncertainty(self):
        text=r.render_markdown(self.report());self.assertIn('original_plan_unavailable',text);self.assertIn('Price-only',text);self.assertIn('Complete 30-session path',text)

    def test_same_timestamp_orders_do_not_claim_inventory_order(self):
        self.fills=[self.fill('one','buy',10,100,0),self.fill('two','sell',10,100,0)]
        self.assertEqual(self.report()['sales'][0]['position_effect'],'unknown_inventory')
    def test_invalid_snapshot_is_ignored(self):
        b=self.report(plans=self.linked_plan(),position_snapshots=[self.snapshot(observed_at='invalid')])['purchases'][0]
        self.assertIsNone(b['filled_plan']['broker_average_entry_after'])

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
