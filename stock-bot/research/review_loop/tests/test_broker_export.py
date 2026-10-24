import hashlib,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from broker_export import (ReadClient,ExportError,NoRedirect,activities,object_pages,check_account,expected_base,scope_actions,collect_book,bootstrap)
ENV={'APCA_API_KEY_ID':'FAKE_KEY_NEVER_PRINT','APCA_API_SECRET_KEY':'FAKE_SECRET_NEVER_PRINT','PAPER_TRADING':'false'}
PIN=hashlib.sha256(b'ACCOUNT_FOR_TEST_ONLY').hexdigest()
class Response:
 status=200
 def __init__(self,payload):self.payload=payload
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def read(self):return json.dumps(self.payload).encode()
class Opener:
 def __init__(self):self.requests=[]
 def open(self,req,timeout):self.requests.append(req);return Response({'id':'ACCOUNT_FOR_TEST_ONLY'})
class Fake:
 def __init__(self,mismatch=False,post_mismatch=False):self.calls=[];self.verified=False;self.mismatch=mismatch;self.post_mismatch=post_mismatch;self.accounts=0
 def get(self,path,params=None,data=False):
  self.calls.append({'path':path,'params':params or {}})
  if path=='/v2/account':
   self.accounts+=1;return {'id':'WRONG' if self.mismatch or (self.post_mismatch and self.accounts>1) else 'ACCOUNT_FOR_TEST_ONLY'}
  if path in ('/v2/positions','/v2/orders'):return []
  if path=='/v2/account/portfolio/history':return {'timestamp':[],'equity':[]}
  if path=='/v2/account/activities':return [{'id':'buy_fill','activity_type':'FILL','order_id':'buy_order','symbol':'TEST','side':'buy','qty':'1','price':'100','transaction_time':'2026-09-01T15:00:00Z'},{'id':'sell_fill','activity_type':'FILL','order_id':'sell_order','symbol':'TEST','side':'sell','qty':'1','price':'105','transaction_time':'2026-09-02T15:00:00Z'}]
  if path=='/v2/calendar':return [{'date':(datetime(2026,9,1)+timedelta(days=i)).date().isoformat(),'close':'16:00'} for i in range(90) if (datetime(2026,9,1)+timedelta(days=i)).weekday()<5]
  if path=='/v1/corporate-actions':return {'corporate_actions':{},'next_page_token':None}
  if path=='/v2/stocks/bars':return {'bars':{'TEST':[{'t':(datetime(2026,9,1)+timedelta(days=i)).date().isoformat()+'T04:00:00Z','c':105,'h':106,'l':104} for i in range(53) if (datetime(2026,9,1)+timedelta(days=i)).weekday()<5]},'next_page_token':None}
  raise AssertionError(path)
class ExportTests(unittest.TestCase):
 def test_no_routes_before_account_verification(self):
  c=ReadClient('https://api.alpaca.markets',ENV)
  with self.assertRaises(ExportError):c.validate('https://api.alpaca.markets/v2/positions')
 def test_wrong_host_and_mutation_routes_are_refused(self):
  c=ReadClient('https://api.alpaca.markets',ENV);c.verified=True
  for u in ['https://evil.example/v2/account','https://paper-api.alpaca.markets/v2/account','https://api.alpaca.markets/v2/orders/123','https://api.alpaca.markets/v2/account/configurations','http://api.alpaca.markets/v2/account','https://user:password@api.alpaca.markets/v2/account']:
   with self.assertRaises(ExportError):c.validate(u)
 def test_transport_only_constructs_get_and_does_not_log_credentials(self):
  o=Opener();c=ReadClient('https://api.alpaca.markets',ENV,opener=o)
  with patch('broker_export.time.sleep'):c.get('/v2/account')
  self.assertEqual(o.requests[0].get_method(),'GET')
  self.assertNotIn('FAKE_KEY',json.dumps(c.calls));self.assertNotIn('FAKE_SECRET',json.dumps(c.calls))
 def test_redirect_is_not_followed_with_credentials(self):
  with self.assertRaises(ExportError):NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example')
 def test_paper_live_flag_mismatch_is_refused(self):
  with self.assertRaises(ExportError):expected_base('alpaca_paper',ENV)
 def test_account_mismatch_stops_before_fills_or_positions(self):
  f=Fake(mismatch=True)
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(ExportError):collect_book('alpaca_live',f,PIN,Path(tmp),Path(tmp),datetime(2026,10,24,tzinfo=timezone.utc))
  self.assertEqual(len(f.calls),1)
 def test_full_page_at_cap_is_truncated_not_complete(self):
  class Full:
   def get(self,*args,**kwargs):return [{'id':str(i)} for i in range(100)]
  rows,cov=activities(Full(),'2026-10-24T00:00:00Z',1)
  self.assertEqual(len(rows),100);self.assertFalse(cov['complete']);self.assertTrue(cov['truncated'])
 def test_conflicting_execution_duplicate_refused(self):
  class Conflict:
   def get(self,*args,**kwargs):return [{'id':'same','price':'1'},{'id':'same','price':'2'}]
  with self.assertRaises(ExportError):activities(Conflict(),'2026-10-24T00:00:00Z',2)
 def test_unscoped_action_does_not_verify_price_followthrough(self):
  p=[{'corporate_actions':{'cash_mergers':[{'effective_date':'2026-09-10'}]}}]
  x=scope_actions(p,True,'TEST','2026-09-01','2026-10-23')
  self.assertFalse(x['corporate_action_evidence']['scoping_verified']);self.assertNotIn('position_changing_action_dates',x)
 def test_split_is_a_price_path_block_not_applied_as_profit(self):
  x=scope_actions([{'corporate_actions':{'forward_splits':[{'symbol':'TEST','ex_date':'2026-09-10'}]}}],True,'TEST','2026-09-01','2026-10-23')
  self.assertEqual(x['position_changing_action_dates'],['2026-09-10'])
 def test_source_pin_is_required_and_mismatch_is_refused(self):
  with self.assertRaises(ExportError):check_account({'id':'WRONG'},PIN)
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(FileNotFoundError):bootstrap(Path(tmp)/'absent',Path(tmp)/'private')
 def test_fresh_collection_preserves_unknown_inventory_and_uses_raw_sip(self):
  f=Fake()
  with tempfile.TemporaryDirectory() as tmp:
   r,receipt=collect_book('alpaca_live',f,PIN,Path(tmp)/'private',Path(tmp),datetime(2026,10,24,tzinfo=timezone.utc))
  self.assertEqual(r['coverage']['initial_inventory'],'unknown');self.assertEqual(r['sales'][0]['position_effect'],'unknown_inventory');self.assertEqual(receipt['fill_count'],2)
  bars=[x for x in f.calls if x['path']=='/v2/stocks/bars'];self.assertEqual(bars[0]['params']['feed'],'sip');self.assertEqual(bars[0]['params']['adjustment'],'raw')
  self.assertNotIn('ACCOUNT_FOR_TEST_ONLY',json.dumps(receipt));self.assertNotIn('buy_order',json.dumps(receipt))
 def test_identity_postcheck_cannot_publish_success(self):
  f=Fake(post_mismatch=True)
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(ExportError):collect_book('alpaca_live',f,PIN,Path(tmp)/'private',Path(tmp),datetime(2026,10,24,tzinfo=timezone.utc))
if __name__=='__main__':unittest.main()
