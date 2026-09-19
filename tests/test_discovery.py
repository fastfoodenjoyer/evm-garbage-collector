import json
import httpx
import pytest
from evm_inventory.discovery import Discovery
from evm_inventory.transport import Transport, RequestError


def test_page_and_address_validation():
    wallet='0x'+'1'*40;token='0x'+'2'*40
    def handler(req):
        assert json.loads(req.content)['addresses'][0]['networks']==['eth-mainnet']
        return httpx.Response(200,json={'data':{'tokens':[{'address':wallet,'network':'eth-mainnet','tokenAddress':token,'tokenBalance':'0x2'}]},'pageKey':'next'})
    d=Discovery(Transport(client=httpx.Client(transport=httpx.MockTransport(handler)),interval=0),key='test-key')
    tokens,cursor=d.page(wallet,'eth-mainnet')
    assert tokens[0]['address']==token and cursor=='next'


def test_partial_errors_not_empty_success():
    def handler(req):return httpx.Response(200,json={'data':{'tokens':[]},'error':{'partialErrors':[{'network':'eth-mainnet'}]}})
    d=Discovery(Transport(client=httpx.Client(transport=httpx.MockTransport(handler)),interval=0),key='key')
    with pytest.raises(RequestError,match='discovery_partial'):d.page('0x'+'1'*40,'eth-mainnet')


def test_no_key_does_not_request():
    d=Discovery(None,key='')
    assert not d.enabled
