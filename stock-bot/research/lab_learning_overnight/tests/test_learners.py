import copy,random,unittest
from learners import fit,predict,learn,VARIANTS,pairs
class LearnerTests(unittest.TestCase):
 def rows(self):
  rng=random.Random(12);rows=[]
  for d in range(1,25):
   for k in range(4):
    x=rng.uniform(.001,.08);rows.append({'x':[x,.002,.6],'y':x*2-.03,'entry_session':f'2025-01-{d:02d}','entry_time':f'2025-01-{d:02d}T10:00:00-05:00','exit_session':f'2025-01-{d:02d}','exit_time':f'2025-01-{d:02d}T15:00:00-05:00'})
  return rows
 def test_all_learners_rank_known_relationship(self):
  rows=self.rows()
  for variant in VARIANTS:
   model=fit(rows,variant);self.assertGreater(predict(model,[.07,.002,.6]),predict(model,[.01,.002,.6]))
 def test_does_not_mutate_baseline_training_outcomes(self):
  rows=self.rows();before=copy.deepcopy(rows)
  for variant in VARIANTS:learn(rows,variant)
  self.assertEqual(rows,before)
 def test_validation_entry_overlap_purges_training_exits(self):
  rows=self.rows()
  for r in rows:
   if r['exit_session']>'2025-01-14':r['entry_time']='2025-01-01T10:00:00-05:00'
  for v in VARIANTS:
   model,info=learn(rows,v);self.assertIsNone(model);self.assertEqual(info['train'],0)
 def test_thin_samples_fall_back(self):
  for v in VARIANTS:self.assertIsNone(learn(self.rows()[:10],v)[0])
 def test_pairwise_compares_only_same_entry_day(self):
  rows=self.rows();self.assertTrue(all(a['entry_session']==b['entry_session'] for a,b,y in pairs(rows)))
 def test_invalid_variant_rejected(self):
  with self.assertRaises(ValueError):learn(self.rows(),'unregistered')
if __name__=='__main__':unittest.main()
