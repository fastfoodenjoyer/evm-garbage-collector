import json
import httpx
import pytest
from evm_inventory.rpc import RpcReader, RpcError, balance_of_data
from evm_inventory.transport import Transport, RequestError


def reader(handler):
    return RpcReader(Transport(client=httpx.Client(transport=httpx.MockTransport(handler)), interval=0,sleep=lambda _:None))


def test_native_zero_and_exact_token():
    def handle(req):
        p=json.loads(req.content);m=p['method']
        result={'eth_chainId':'0xa','eth_getBalance':'0x0','eth_getCode':'0x6000'}.get(m)
        if m=='eth_call': result='0x'+format(6 if p['params'][0]['data']=='0x313ce567' else 2**256-1,'064x')
        return httpx.Response(200,json={'jsonrpc':'2.0','id':p['id'],'result':result})
    r=reader(handle)
    assert r.native('https://rpc.test',10,'0x'+'1'*40,123)==0
    assert r.token('https://rpc.test',10,'0x'+'1'*40,'0x'+'2'*40,123,6)==(2**256-1,6)
    assert len(balance_of_data('0x'+'1'*40))==74


def test_wrong_chain_and_absent_code_are_errors():
    def handle(req):
        p=json.loads(req.content)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':p['id'],'result':'0x0'})
    r=reader(handle)
    with pytest.raises(RpcError,match='wrong_chain'):r.native('https://rpc.test',10,'0x'+'1'*40,2)


def test_malformed_abi_not_zero():
    def handle(req):
        p=json.loads(req.content)
        result={'eth_chainId':'0xa','eth_getCode':'0x6000','eth_call':'0x'}.get(p['method'])
        return httpx.Response(200,json={'jsonrpc':'2.0','id':p['id'],'result':result})
    with pytest.raises(RpcError):reader(handle).token('https://rpc.test',10,'0x'+'1'*40,'0x'+'2'*40,1,6)


def test_retries_and_redacts_secrets():
    calls=[]
    def handler(req):
        calls.append(1)
        return httpx.Response(429,headers={'Retry-After':'0'})
    t=Transport(client=httpx.Client(transport=httpx.MockTransport(handler)),interval=0,sleep=lambda _:None)
    with pytest.raises(RequestError) as err:t.post('https://rpc.test/secret',{})
    assert len(calls)==3
    assert 'secret' not in str(err.value)


def test_http_timeout_retries_success():
    calls=[]
    def handler(req):
        calls.append(1)
        if len(calls)==1:return httpx.Response(408)
        return httpx.Response(200,json={'ok':True})
    t=Transport(client=httpx.Client(transport=httpx.MockTransport(handler)),interval=0,sleep=lambda _:None)
    assert t.post('https://rpc.test',{})=={'ok':True}
    assert len(calls)==2


def test_bad_expanded_url_sanitised(monkeypatch):
    monkeypatch.setenv('RPC_URL','https://[secret')
    t=Transport(interval=0)
    with pytest.raises(RequestError,match='invalid_rpc_url'):t.post('${RPC_URL}',{})
