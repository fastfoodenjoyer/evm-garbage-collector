"""Atomic CSV and JSON exports."""
import csv
import json
import os
import tempfile
from pathlib import Path


def _safe(v):
    s='' if v is None else str(v)
    return "'"+s if s[:1] in '=+-@' else s

def export_run(store, run_id, output: Path):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    marker=output/'.inventory-run'; existing=marker.read_text().strip() if marker.exists() else None
    if existing and existing != run_id: raise ValueError('output belongs to another run')
    marker.write_text(run_id+'\n')
    run=store.run(run_id); jobs=store.jobs(run_id); scope=run['snapshot']
    rows=[]
    for j in jobs:
        r=j.get('result') or {}
        rows.append({'wallet':j['wallet'],'chain_id':j['chain_id'],'asset_id':j['asset_id'],
          'symbol':r.get('symbol') or j.get('metadata',{}).get('symbol',''),'raw_balance':r.get('raw_balance'),
          'decimals':r.get('decimals'),'amount':r.get('amount'),'status':j['status'],'error':r.get('error'),
          'block_number':r.get('block_number'),'observed_at':r.get('observed_at'),'price_usd':r.get('price_usd')})
    checks=rows
    balances=[r for r in rows if r['status']=='success' and r['raw_balance'] is not None and int(r['raw_balance'])>0]
    coverage=[]
    for wallet in scope['wallets']:
        for n in scope['catalog']['networks']:
            subset=[r for r in rows if r['wallet']==wallet and r['chain_id']==n['chain_id'] and r['asset_id']!='discovery']
            coverage.append({'wallet':wallet,'chain_id':n['chain_id'],'network':n['name'],
              'mandatory_status':'complete' if subset and all(x['status']=='success' for x in subset) else 'incomplete',
              'token_review_status':n.get('token_review_status','pending')})
    def write_csv(name,data,fields):
        fd,tmp=tempfile.mkstemp(dir=output,prefix='.__tmp-',text=True); os.close(fd)
        try:
            with open(tmp,'w',newline='',encoding='utf8') as f:
                w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows({k:_safe(x.get(k)) for k in fields} for x in data)
            os.replace(tmp,output/name)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    fields=['wallet','chain_id','asset_id','symbol','raw_balance','decimals','amount','status','error','block_number','observed_at','price_usd']
    write_csv('balances.csv',balances,fields); write_csv('checks.csv',checks,fields)
    write_csv('coverage.csv',coverage,['wallet','chain_id','network','mandatory_status','token_review_status'])
    payload={'run':run,'balances':balances,'checks':checks,'coverage':coverage}
    fd,tmp=tempfile.mkstemp(dir=output,prefix='.__tmp-',text=True); os.close(fd)
    try:
        Path(tmp).write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n'); os.replace(tmp,output/'inventory.json')
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
