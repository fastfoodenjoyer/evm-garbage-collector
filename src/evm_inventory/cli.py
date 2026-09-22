"""Command-line interface for EVM inventory scans."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

import httpx

from .config import add_allowlisted_tokens, load_catalog, load_wallets, snapshot, validate_delays
from .lifi import LifiClient
from .live_plan import create_live_plan
from .models import ConfigError
from .planner_gas import PlannerGasEstimator
from .report import export_current
from .route_execution import execute_entries, resume_routes_read_only
from .routing_settings import load_routing_settings
from .rpc import RpcReader
from .runbook import create_route_plan
from .scanner import scan
from .store import Store
from .transport import Transport
from .workbook import (
    create_wallet_template,
    dry_run_workbook,
    load_wallet_workbook,
    parse_ordinal_ranges,
    wallet_range_batches,
)

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _dotenv_value(raw: str, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in "\"'":
        quote = value[0]
        end = value.find(quote, 1)
        remainder = value[end + 1 :].strip() if end >= 0 else "invalid"
        if end < 0 or (remainder and not remainder.startswith("#")):
            raise ConfigError(f"invalid .env entry at line {line_number}")
        return value[1:end]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _load_dotenv() -> None:
    """Load literal KEY=value entries from ``.env`` in the current directory."""

    path = Path.cwd() / ".env"
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as exc:
        raise ConfigError(".env file must be valid UTF-8") from exc
    except OSError as exc:
        raise ConfigError("cannot read .env file") from exc
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"invalid .env entry at line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key):
            raise ConfigError(f"invalid .env entry at line {line_number}")
        os.environ.setdefault(key, _dotenv_value(raw_value, line_number))


def _summary(result: dict, catalog, *, key_present: bool) -> dict:
    mapped = sum(bool(network.alchemy_network) for network in catalog.networks)
    gaps = sum(network.token_review_status == "pending" for network in catalog.networks)
    return {
        **result,
        "catalog_gaps": result.get("catalog_gaps", gaps),
        "alchemy_mapped_networks": mapped,
        "alchemy_key_present": bool(key_present),
    }


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main(argv=None):
    try:
        _load_dotenv()
        parser = argparse.ArgumentParser(prog="evm-inventory")
        sub = parser.add_subparsers(dest="cmd", required=True)
        scan_parser = sub.add_parser("scan")
        scan_parser.add_argument("--wallets", required=True)
        scan_parser.add_argument("--db", required=True)
        scan_parser.add_argument("--catalog")
        scan_parser.add_argument("--allowlist", default="config/swap-allowlist.json")
        scan_parser.add_argument("--delay-min", type=float, default=1)
        scan_parser.add_argument("--delay-max", type=float, default=3)
        scan_parser.add_argument("--interval", type=float, default=1)
        scan_parser.add_argument("--no-discovery", action="store_true")
        scan_parser.add_argument("--dry-run", action="store_true")
        export_parser = sub.add_parser("export")
        export_parser.add_argument("--db", required=True)
        export_parser.add_argument("--output", required=True)
        template_parser = sub.add_parser("workbook-template")
        template_parser.add_argument("--output", required=True)
        workbook_dry_run_parser = sub.add_parser("workbook-dry-run")
        workbook_dry_run_parser.add_argument("--workbook", required=True)
        route_plan_parser = sub.add_parser("route-plan")
        route_plan_parser.add_argument("--balances", required=True)
        route_plan_parser.add_argument("--output", required=True)
        route_plan_parser.add_argument("--quote-floor-raw", type=int, default=100)
        quote_routes_parser = sub.add_parser("quote-routes")
        quote_routes_parser.add_argument("--balances", required=True)
        quote_routes_parser.add_argument("--workbook", required=True)
        quote_routes_parser.add_argument("--output", required=True)
        quote_routes_parser.add_argument("--allowlist", default="config/swap-allowlist.json")
        quote_routes_parser.add_argument("--catalog")
        quote_routes_parser.add_argument("--quote-floor", default="0.01")
        quote_routes_parser.add_argument(
            "--wallet-ranges",
            help="inclusive workbook ordinals, e.g. 1-50,75,100-120",
        )
        execute_parser = sub.add_parser("execute-routes")
        execute_parser.add_argument("--plan", required=True)
        execute_parser.add_argument("--workbook", required=True)
        execute_parser.add_argument("--catalog", required=True)
        execute_parser.add_argument("--journal", required=True)
        execute_parser.add_argument("--execute", action="store_true")
        execute_parser.add_argument(
            "--wallet-ranges",
            help="inclusive workbook ordinals, e.g. 1-50,75,100-120",
        )
        resume_routes_parser = sub.add_parser("resume-routes")
        resume_routes_parser.add_argument("--journal", required=True)
        args = parser.parse_args(argv)

        if args.cmd == "workbook-template":
            output = Path(args.output)
            create_wallet_template(output)
            print(json.dumps({"status": "created", "workbook": str(output)}))
            return 0
        if args.cmd == "workbook-dry-run":
            print(json.dumps(dry_run_workbook(Path(args.workbook))))
            return 0
        if args.cmd == "route-plan":
            plan = create_route_plan(Path(args.balances), quote_floor_raw=args.quote_floor_raw)
            Path(args.output).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": "planned", "output": str(args.output), **plan["summary"]}))
            return 0
        if args.cmd == "quote-routes":
            routing_settings = load_routing_settings()
            ordinal_ranges = parse_ordinal_ranges(args.wallet_ranges)
            wallet_rows = load_wallet_workbook(
                Path(args.workbook),
                require_deposit_address=False,
                ordinal_ranges=ordinal_ranges,
            )
            wallet_range_batches(wallet_rows, ordinal_ranges)
            addresses = {
                item.public_address: item.bitget_deposit_address for item in wallet_rows
            }
            catalog = load_catalog(Path(args.catalog) if args.catalog else None)
            transport = Transport()
            lifi_http = httpx.Client(timeout=30)
            quote_client = LifiClient(lifi_http)
            try:
                gas_estimator = PlannerGasEstimator(
                    RpcReader(transport), _execution_rpc_urls(catalog), quote_client
                )
                plan = create_live_plan(
                    Path(args.balances),
                    deposit_addresses=addresses,
                    allowlist_path=Path(args.allowlist),
                    quote_floor=args.quote_floor,
                    wallet_addresses=set(addresses),
                    max_route_loss_pct=routing_settings.max_route_loss_pct,
                    client=quote_client,
                    gas_estimator=gas_estimator,
                )
            finally:
                lifi_http.close()
                transport.close()
            Path(args.output).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": "quoted", "output": str(args.output), **plan["summary"]}))
            return 0
        if args.cmd == "execute-routes":
            routing_settings = load_routing_settings()
            ordinal_ranges = parse_ordinal_ranges(args.wallet_ranges)
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            if not isinstance(plan, dict) or not isinstance(plan.get("entries"), list):
                raise ConfigError("invalid route plan")
            catalog = load_catalog(Path(args.catalog))
            rpc_urls = _execution_rpc_urls(catalog)
            wallet_rows = load_wallet_workbook(
                Path(args.workbook), ordinal_ranges=ordinal_ranges
            )
            batches = wallet_range_batches(wallet_rows, ordinal_ranges)
            wallet_map = {item.public_address: item for item in wallet_rows}
            selected_wallets = set(wallet_map)
            entries = [
                entry
                for entry in plan["entries"]
                if str(entry.get("wallet", "")).lower() in selected_wallets
            ]
            wallet_batches = tuple(
                tuple(row.public_address for row in batch) for batch in batches
            )
            execution = plan.get("execution", {})
            summary = execute_entries(
                entries,
                wallets=wallet_map,
                rpc_urls=rpc_urls,
                journal_path=Path(args.journal),
                execute=args.execute,
                delay_min_seconds=int(execution.get("delay_min_seconds", 1800)),
                delay_max_seconds=int(execution.get("delay_max_seconds", 10800)),
                wallet_batches=wallet_batches,
                max_route_loss_pct=routing_settings.max_route_loss_pct,
            )
            print(json.dumps({"status": "finished", **summary}))
            return 0
        if args.cmd == "resume-routes":
            print(json.dumps(resume_routes_read_only(journal_path=Path(args.journal))))
            return 0
        if args.cmd == "export":
            with Store(args.db, readonly=True) as store:
                export_current(store, Path(args.output))
            return 0
        if args.cmd == "scan":
            wallets = load_wallets(Path(args.wallets))
            catalog = load_catalog(Path(args.catalog) if args.catalog else None)
            delay_min, delay_max = validate_delays(args.delay_min, args.delay_max)
            if not math.isfinite(args.interval) or args.interval < 0:
                raise ConfigError("interval must be finite and nonnegative")
            key_present = bool(os.environ.get("ALCHEMY_API_KEY"))
            settings = {
                "delay_min": delay_min,
                "delay_max": delay_max,
                "interval": float(args.interval),
                "discovery_enabled": key_present and not args.no_discovery,
            }
            scope = snapshot(catalog, wallets, settings)
            scope = add_allowlisted_tokens(scope, Path(args.allowlist))
            if args.dry_run:
                checks_per_wallet = sum(
                    1 + len(network["tokens"]) for network in scope["catalog"]["networks"]
                )
                result = {
                    "wallets": len(wallets),
                    "networks": len(catalog.networks),
                    "mandatory_checks": len(wallets) * checks_per_wallet,
                }
                print(json.dumps(_summary(result, catalog, key_present=key_present)))
                return 0
            with Store(args.db) as store:
                result = scan(store, scope, progress=_progress)
            print(json.dumps(_summary(result, catalog, key_present=key_present)))
            return 0 if result.get("status", "completed") == "completed" else 3
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}), file=sys.stderr, flush=True)
        return 130
    except sqlite3.OperationalError:
        print("error: database operation failed", file=sys.stderr)
        return 4
    except (ConfigError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

def _execution_rpc_urls(catalog) -> dict[int, str]:
    """Prefer Alchemy for execution preflight and broadcast when it maps the chain."""

    key = os.environ.get("ALCHEMY_RPC_API_KEY") or os.environ.get("ALCHEMY_API_KEY")
    urls: dict[int, str] = {}
    for network in catalog.networks:
        if key and network.alchemy_network:
            urls[network.chain_id] = f"https://{network.alchemy_network}.g.alchemy.com/v2/{key}"
        else:
            urls[network.chain_id] = network.rpc_urls[0]
    return urls
