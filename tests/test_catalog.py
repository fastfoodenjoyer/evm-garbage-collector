import pytest
from scripts.update_registry import build_candidate


def test_mainnets_dedup_and_preserve_tokens():
    rows=[{'identifier':'mainnet/op','chainId':10,'name':'OP','rpc':['https://mainnet.optimism.io']},
          {'identifier':'sepolia/op','chainId':11155420,'name':'OP test','rpc':[]},
          {'identifier':'mainnet/base','chainId':8453,'name':'Base','rpc':['https://mainnet.base.org']}]
    old={'networks':[{'chain_id':10,'tokens':[{'address':'a'}],'token_review_status':'verified'}]}
    got=build_candidate(rows, 'a'*40, '2026-09-19', old)
    ids=[n['chain_id'] for n in got['networks']]
    assert sorted(ids)==[1,10,8453,42161,81457]
    assert next(n for n in got['networks'] if n['chain_id']==10)['tokens']==[{'address':'a'}]
    assert next(n for n in got['networks'] if n['chain_id']==8453)['token_review_status']=='pending'


def test_registry_rejects_bad_rows():
    with pytest.raises(ValueError):
        build_candidate([{'name':'bad'}], 'a'*40, '2026-09-19', {})


def test_no_arbitrary_native_eth_for_custom_gas():
    row={'identifier':'mainnet/custom','chainId':55,'name':'Custom','rpc':[], 'gasPayingToken':'0x'+'1'*40}
    got=build_candidate([row], 'a'*40, '2026-09-19', {})
    assert next(n for n in got['networks'] if n['chain_id']==55)['native_symbol']=='UNKNOWN'
