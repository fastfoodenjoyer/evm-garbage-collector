from evm_inventory.scanner import scan
from evm_inventory.store import Store

W='0x'+'1'*40
T='0x'+'2'*40

def scope():
    return {'wallets':[W], 'settings':{'delay_min':0,'delay_max':0},'catalog':{'networks':[
        {'chain_id':10,'name':'OP','rpc_urls':['https://rpc.test'],'native_symbol':'ETH','native_decimals':18,'token_review_status':'verified','alchemy_network':None,
         'tokens':[{'address':T,'symbol':'USDC','decimals':6,'source':'https://example.com','checked_at':'2026-09-19','variant':''}]}]}}

class RPC:
    def block(self,url,chain_id,number=None):return {'number':1,'hash':'0x'+'a'*64,'timestamp':1}
    def native(self,*args):return 0
    def token(self,*args):return 1230000,6


def test_zero_native_still_checks_stable_resume_skips(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    run=store.create_run(scope())
    result=scan(store,run,rpc=RPC(),sleep=lambda _:None)
    assert result['status']=='completed'
    jobs=store.jobs(run)
    assert next(j for j in jobs if j['asset_id']==T)['result']['raw_balance']=='1230000'
    assert next(j for j in jobs if j['asset_id']=='native')['result']['raw_balance']=='0'
    result=scan(store,run,rpc=RPC(),sleep=lambda _:None)
    assert all(j['attempts']==1 for j in store.jobs(run) if j['kind']=='mandatory')
    store.close()


def test_network_failure_creates_all_checks(tmp_path):
    s=scope();s['catalog']['networks'][0]['rpc_urls']=[]
    store=Store(tmp_path/'db.sqlite');run=store.create_run(s)
    assert scan(store,run,rpc=RPC())['status']=='incomplete'
    jobs=[j for j in store.jobs(run) if j['kind']=='mandatory']
    assert len(jobs)==2 and all(j['status']=='unavailable' for j in jobs)
    store.close()
