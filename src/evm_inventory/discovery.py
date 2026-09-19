"""Optional Alchemy ERC-20 discovery; balances are verified separately via RPC."""
import os
import re
from datetime import UTC, datetime
from urllib.parse import quote

from .transport import RequestError

SOURCE = 'https://www.alchemy.com/docs/data/portfolio-apis/portfolio-api-endpoints/portfolio-api-endpoints/get-token-balances-by-address'
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')


class Discovery:
    def __init__(self,transport,key=None):
        self.transport=transport
        self.key=os.environ.get('ALCHEMY_API_KEY','') if key is None else key
        self.enabled=bool(self.key)
        self.stopped=None

    def page(self,wallet,network,cursor=None):
        if not self.enabled:
            raise RequestError('discovery_disabled')
        if self.stopped:
            raise RequestError(self.stopped)
        payload={'addresses':[{'address':wallet,'networks':[network]}],
                 'includeNativeTokens':False,'includeErc20Tokens':True,'includeBlockMetadata':False}
        if cursor:
            payload['pageKey']=cursor
        url='https://api.g.alchemy.com/data/v1/'+quote(self.key,safe='')+'/assets/tokens/balances/by-address'
        try:
            data=self.transport.post(url,payload)
        except RequestError as exc:
            if exc.code=='provider_access_denied':
                self.stopped=exc.code
            raise
        # Network errors are independent of HTTP status and of per-token metadata errors.
        if data.get('error'):
            raise RequestError('discovery_partial')
        if not isinstance(data.get('data'),dict) or not isinstance(data['data'].get('tokens'),list):
            raise RequestError('discovery_invalid_response')
        result={}
        for token in data['data']['tokens']:
            if not isinstance(token,dict):
                raise RequestError('discovery_invalid_token')
            contract=token.get('tokenAddress')
            if contract is None:
                continue
            if not isinstance(contract,str) or not ADDRESS.fullmatch(contract):
                raise RequestError('discovery_invalid_contract')
            if token.get('address','').lower()!=wallet.lower() or token.get('network')!=network:
                raise RequestError('discovery_wrong_scope')
            result[contract.lower()]={'address':contract.lower(),'symbol':contract.lower(),
                                      'decimals':None,'source':SOURCE,
                                      'checked_at':datetime.now(UTC).date().isoformat(),
                                      'variant':'discovered ERC-20'}
        next_cursor=data.get('pageKey')
        if next_cursor is not None and (not isinstance(next_cursor,str) or not next_cursor):
            raise RequestError('discovery_invalid_cursor')
        return list(result.values()),next_cursor
