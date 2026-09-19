"""Read-only EVM RPC and strict ERC-20 result decoding."""
import itertools
import re

from .transport import RequestError, Transport


class RpcError(RequestError):
    pass


def balance_of_data(address: str) -> str:
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', address):
        raise RpcError('invalid_wallet')
    return '0x70a08231' + address[2:].lower().rjust(64, '0')


def quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)', value):
        raise RpcError('invalid_quantity')
    return int(value, 16)


def uint256(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', value):
        raise RpcError('invalid_abi_uint256')
    return int(value, 16)


class RpcReader:
    def __init__(self, transport: Transport):
        self.transport = transport
        self.ids = itertools.count(1)
        self.identities = set()
        self.metadata = {}

    def call(self, url, method, params):
        if method not in {'eth_chainId','eth_getBlockByNumber','eth_getBalance','eth_getCode','eth_call'}:
            raise RpcError('method_not_allowed')
        ident = next(self.ids)
        result = self.transport.post(url, {'jsonrpc':'2.0','id':ident,'method':method,'params':params})
        if result.get('id') != ident or result.get('jsonrpc') != '2.0':
            raise RpcError('invalid_rpc_envelope')
        if result.get('error'):
            # Provider message can include secrets or untrusted text; retain only numeric code.
            raise RpcError('rpc_error')
        if 'result' not in result:
            raise RpcError('missing_rpc_result')
        return result['result']

    def check_chain(self, url, chain_id):
        if (url, chain_id) not in self.identities:
            if quantity(self.call(url,'eth_chainId',[])) != chain_id:
                raise RpcError('wrong_chain')
            self.identities.add((url,chain_id))

    def block(self, url, chain_id, number=None):
        self.check_chain(url,chain_id)
        result = self.call(url,'eth_getBlockByNumber',[hex(number) if number is not None else 'latest',False])
        if not isinstance(result,dict):
            raise RpcError('block_unavailable')
        block_num = quantity(result.get('number'))
        block_hash = result.get('hash')
        if not isinstance(block_hash,str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}',block_hash):
            raise RpcError('invalid_block_hash')
        if number is not None and block_num != number:
            raise RpcError('wrong_block')
        return {'number':block_num,'hash':block_hash.lower(),'timestamp':quantity(result.get('timestamp'))}

    def native(self,url,chain_id,wallet,block):
        balance_of_data(wallet)
        self.check_chain(url,chain_id)
        value=quantity(self.call(url,'eth_getBalance',[wallet,hex(block)]))
        if value >= 2**256:
            raise RpcError('invalid_balance')
        return value

    def token(self,url,chain_id,wallet,contract,block,expected_decimals=None):
        balance_of_data(wallet)
        if not isinstance(contract, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}',contract):
            raise RpcError('invalid_contract')
        self.check_chain(url,chain_id)
        key=(url,chain_id,contract.lower(),block)
        if key not in self.metadata:
            code=self.call(url,'eth_getCode',[contract,hex(block)])
            if not isinstance(code,str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})+',code):
                raise RpcError('missing_contract_code')
            try:
                decimals=uint256(self.call(url,'eth_call',[{'to':contract,'data':'0x313ce567'},hex(block)]))
                if decimals > 255:
                    raise RpcError('invalid_decimals')
            except RequestError:
                if expected_decimals is not None:
                    raise
                decimals=None
            self.metadata[key]=decimals
        decimals=self.metadata[key]
        if expected_decimals is not None and decimals!=expected_decimals:
            raise RpcError('decimals_mismatch')
        raw=uint256(self.call(url,'eth_call',[{'to':contract,'data':balance_of_data(wallet)},hex(block)]))
        return raw,decimals
