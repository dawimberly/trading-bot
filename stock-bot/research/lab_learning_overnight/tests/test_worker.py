import unittest
from worker import report
class WorkerTests(unittest.TestCase):
 def test_higher_cost_tie_cannot_promote_nominal_winner(self):
  base={'return':.05,'max_drawdown':.1,'first_half':.02,'second_half':.03,'all147_return':.06};good={'return':.10,'max_drawdown':.08,'first_half':.04,'second_half':.06,'all147_return':.11}
  r={'status':'COMPLETED','updated_at':'2026-10-24T00:00:00Z','jobs':[{'variant':'pairwise_rank','cost_bps':5,'status':'COMPLETED','result':{'metrics':{'baseline':base,'learner':good},'ledger_reconciled':True,'data_defects':[]}},{'variant':'pairwise_rank','cost_bps':10,'status':'COMPLETED','result':{'metrics':{'baseline':base,'learner':base},'ledger_reconciled':True,'data_defects':[]}}]}
  out=report(r);self.assertEqual(out['candidates'][0]['status'],'reject');self.assertEqual(out['lab_better_than_live_and_medium'],'NOT_PROVEN');self.assertFalse(out['promotion'])
 def test_failed_cell_remains_blocked_not_a_winner(self):
  out=report({'status':'RUNNING','updated_at':'2026-10-24T00:00:00Z','jobs':[{'variant':'pairwise_rank','cost_bps':5,'status':'BLOCKED'}]});self.assertEqual(out['completed_jobs'],0);self.assertTrue(all(x['status']=='pending_or_blocked' for x in out['candidates']))
if __name__=='__main__':unittest.main()
