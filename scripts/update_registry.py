"""Generate a candidate catalog from an explicit Superchain registry revision."""
import argparse
import json
import re
from datetime import date
from pathlib import Path
from urllib.request import urlopen

EXTRAS = [
    (1, 'Ethereum Mainnet', ['https://ethereum-rpc.publicnode.com']),
    (42161, 'Arbitrum One', ['https://arb1.arbitrum.io/rpc']),
    (8453, 'Base', ['https://mainnet.base.org']),
    (81457, 'Blast', ['https://rpc.blast.io']),
]
NATIVE = {65536:'ATA',624:'BNRY',177:'HSK',42220:'CELO',252:'frxETH'}


def build_candidate(rows, revision, checked_at, previous):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('revision must be an exact commit SHA')
    old={n['chain_id']:n for n in previous.get('networks', [])}
    nets={}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('identifier'), str):
            raise ValueError('invalid registry row')
        if row['identifier'].split('/')[0] != 'mainnet':
            continue
        cid=row.get('chainId')
        if type(cid) is not int or cid<=0 or not row.get('name') or not isinstance(row.get('rpc'),list):
            raise ValueError('invalid mainnet record')
        if cid in nets:
            raise ValueError('duplicate registry chain ID')
        nets[cid]={'chain_id':cid, 'name':row['name'], 'rpc_urls':row['rpc'],
                   'native_symbol':NATIVE.get(cid, 'UNKNOWN' if row.get('gasPayingToken') else 'ETH'),
                   'native_decimals':18, 'tokens':[], 'token_review_status':'pending',
                   'notes':'Stablecoin catalog review pending.', 'alchemy_network':None}
    for cid,name,rpcs in EXTRAS:
        nets.setdefault(cid, {'chain_id':cid,'name':name,'rpc_urls':rpcs,'native_symbol':'ETH',
                             'native_decimals':18,'tokens':[],'token_review_status':'pending',
                             'notes':'Stablecoin catalog review pending.', 'alchemy_network':None})
    for cid,net in nets.items():
        if cid in old:
            for key in ('tokens','token_review_status','notes','alchemy_network','native_symbol','native_decimals'):
                if key in old[cid]:
                    net[key]=old[cid][key]
    return {'revision':revision, 'checked_at':checked_at,
            'source':f'https://github.com/ethereum-optimism/superchain-registry/tree/{revision}',
            'networks':sorted(nets.values(),key=lambda n:n['chain_id'])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--previous',type=Path)
    args=parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}',args.revision):
        parser.error('--revision must be exact 40-character commit SHA')
    if args.output.exists():
        parser.error('candidate output already exists; choose a new file')
    url=f'https://raw.githubusercontent.com/ethereum-optimism/superchain-registry/{args.revision}/chainList.json'
    with urlopen(url,timeout=20) as response:
        rows=json.load(response)
    old=json.loads(args.previous.read_text()) if args.previous else {}
    candidate=build_candidate(rows,args.revision,date.today().isoformat(),old)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as output:
        json.dump(candidate,output,indent=2)
        output.write('\n')
    print(f'Candidate: {args.output}; {len(candidate["networks"])} networks. Review before use.')


if __name__=='__main__':
    main()
