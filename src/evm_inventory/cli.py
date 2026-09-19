import argparse
import json
import os
import sys
from pathlib import Path

from .config import load_catalog, load_wallets, snapshot, validate_delays
from .report import export_run
from .scanner import scan
from .store import Store


def _load_dotenv():
    path = Path.cwd() / ".env"
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.isidentifier():
            os.environ.setdefault(key, value.strip().strip("\"'"))


def main(argv=None):
    _load_dotenv()
    p=argparse.ArgumentParser(prog='evm-inventory'); sub=p.add_subparsers(dest='cmd',required=True)
    s=sub.add_parser('scan'); s.add_argument('--wallets',required=True); s.add_argument('--db',required=True); s.add_argument('--catalog'); s.add_argument('--delay-min',type=float,default=1); s.add_argument('--delay-max',type=float,default=3); s.add_argument('--dry-run',action='store_true')
    r=sub.add_parser('resume'); r.add_argument('--run',required=True); r.add_argument('--db',required=True)
    e=sub.add_parser('export'); e.add_argument('--run',required=True); e.add_argument('--db',required=True); e.add_argument('--output',required=True)
    try:
        a=p.parse_args(argv)
        if a.cmd=='export':
            with Store(a.db,readonly=True) as st: export_run(st,a.run,Path(a.output)); return 0
        if a.cmd=='scan':
            wallets=load_wallets(Path(a.wallets)); catalog=load_catalog(Path(a.catalog) if a.catalog else None); dmin,dmax=validate_delays(a.delay_min,a.delay_max)
            settings={'delay_min':dmin,'delay_max':dmax,'interval':1,'discovery_enabled':bool(__import__('os').environ.get('ALCHEMY_API_KEY'))}
            scope=snapshot(catalog,wallets,settings)
            if a.dry_run:
                count=len(wallets)*len(catalog.networks); tokens=sum(len(n.tokens)+1 for n in catalog.networks)
                print(json.dumps({'wallets':len(wallets),'networks':len(catalog.networks),'mandatory_checks':count*tokens})); return 0
            with Store(a.db) as st:
                run=st.create_run(scope); print(run,flush=True); result=scan(st,run)
                return 0 if result['status']=='completed' else 3
        with Store(a.db) as st: result=scan(st,a.run,resume=True); return 0 if result['status']=='completed' else 3
    except (ValueError,OSError) as exc:
        print(f'error: {exc}',file=sys.stderr); return 2
