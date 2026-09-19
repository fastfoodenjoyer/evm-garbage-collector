"""Atomic, scope-aware exports for inventory runs."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

_FILES = ("balances.csv", "checks.csv", "coverage.csv", "inventory.json")
_CHECK_FIELDS = [
    "wallet",
    "chain_id",
    "asset_id",
    "name",
    "symbol",
    "raw_balance",
    "decimals",
    "amount",
    "status",
    "error",
    "block_number",
    "observed_at",
    "price_usd",
    "price_source",
    "price_timestamp",
    "source",
    "verification",
]


def _safe(value: Any) -> Any:
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text and text[:1] in "=+-@" else text


def _row(job: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    result = job.get("result") or {}
    meta = metadata or job.get("metadata") or {}
    raw = result.get("raw_balance")
    return {
        "wallet": job["wallet"],
        "chain_id": job["chain_id"],
        "asset_id": job["asset_id"],
        "name": result.get("name") or meta.get("name", ""),
        "symbol": result.get("symbol") or meta.get("symbol", ""),
        "raw_balance": str(raw) if raw is not None else None,
        "decimals": result.get("decimals", meta.get("decimals")),
        "amount": result.get("amount"),
        "status": job.get("status", "pending"),
        "error": result.get("error"),
        "block_number": result.get("block_number"),
        "observed_at": result.get("observed_at"),
        "price_usd": result.get("price_usd"),
        "price_source": result.get("price_source"),
        "price_timestamp": result.get("price_timestamp"),
        "source": result.get("source"),
        "verification": result.get("verification"),
    }


def _expected(scope: dict[str, Any]) -> dict[tuple[str, int, str], dict[str, Any]]:
    expected: dict[tuple[str, int, str], dict[str, Any]] = {}
    for wallet in scope.get("wallets", ()):
        for network in scope.get("catalog", {}).get("networks", ()):
            cid = network["chain_id"]
            expected[(wallet, cid, "native")] = {
                "symbol": network.get("native_symbol", ""),
                "decimals": network.get("native_decimals"),
            }
            for token in network.get("tokens", ()):
                expected[(wallet, cid, token["address"])] = token
    return expected


def _preflight(output: Path, run_id: str) -> None:
    if not output.exists():
        output.mkdir(parents=True)
        return
    marker = output / ".inventory-run"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() != run_id:
        raise ValueError("output belongs to another run")
    allowed = set(_FILES) | {".inventory-run"}
    if [entry for entry in output.iterdir() if entry.name not in allowed]:
        raise ValueError("output contains unrelated files")
    if not marker.exists() and any((output / name).exists() for name in _FILES):
        raise ValueError("output is not a managed inventory export")


def _write_csv(output: Path, name: str, rows: list[dict[str, Any]], fields: list[str]) -> Path:
    fd, temporary = tempfile.mkstemp(dir=output, prefix=".__tmp-", text=True)
    os.close(fd)
    try:
        with open(temporary, "w", newline="", encoding="utf8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows({field: _safe(row.get(field)) for field in fields} for row in rows)
        return Path(temporary)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _decimal_total(rows: list[dict[str, Any]]) -> tuple[str | None, int, int]:
    total = Decimal(0)
    priced = unpriced = 0
    with localcontext() as context:
        context.prec = 1000
        for row in rows:
            if row.get("price_usd") is None:
                unpriced += 1
                continue
            try:
                amount = row.get("amount")
                if amount is None:
                    decimals = row.get("decimals")
                    if decimals is None:
                        unpriced += 1
                        continue
                    amount = Decimal(row["raw_balance"]) / (Decimal(10) ** int(decimals))
                price = Decimal(str(row["price_usd"]))
                if not price.is_finite() or price < 0:
                    unpriced += 1
                    continue
                total += Decimal(str(amount)) * price
            except (InvalidOperation, ValueError):
                unpriced += 1
                continue
            priced += 1
    return (format(total, "f") if priced else None), priced, unpriced


def export_run(store, run_id: str, output: Path):
    output = Path(output)
    _preflight(output, run_id)
    run = store.run(run_id)
    scope = run["snapshot"]
    expected = _expected(scope)
    all_jobs = store.jobs(run_id)
    scope_pairs = {
        (wallet, network["chain_id"])
        for wallet in scope.get("wallets", ())
        for network in scope.get("catalog", {}).get("networks", ())
    }
    scoped_jobs = [j for j in all_jobs if (j["wallet"], j["chain_id"]) in scope_pairs]
    by_key = {(j["wallet"], j["chain_id"], j["asset_id"]): j for j in scoped_jobs}
    checks = []
    for key, metadata in expected.items():
        job = by_key.get(key)
        if job is None:
            wallet, chain_id, asset_id = key
            job = {
                "wallet": wallet,
                "chain_id": chain_id,
                "asset_id": asset_id,
                "status": "pending",
                "metadata": metadata,
                "result": None,
            }
        checks.append(_row(job, metadata))
    discovered = [_row(j) for j in scoped_jobs if j.get("kind") == "discovered"]
    balances = [
        r
        for r in checks + discovered
        if r["status"] in {"success", "provider_only"}
        and r["raw_balance"] is not None
        and int(r["raw_balance"]) > 0
    ]

    coverage = []
    for wallet in scope.get("wallets", ()):
        for network in scope.get("catalog", {}).get("networks", ()):
            cid = network["chain_id"]
            mandatory = [r for r in checks if r["wallet"] == wallet and r["chain_id"] == cid]
            discovery = next(
                (
                    j
                    for j in scoped_jobs
                    if j["wallet"] == wallet
                    and j["chain_id"] == cid
                    and j.get("kind") == "discovery"
                ),
                None,
            )
            discovery_status = discovery.get("status", "pending") if discovery else "pending"
            verification_failures = sum(
                1
                for j in scoped_jobs
                if j["wallet"] == wallet
                and j["chain_id"] == cid
                and j.get("kind") == "discovered"
                and j.get("status") != "success"
            )
            review = network.get("token_review_status", "pending")
            complete = bool(mandatory) and all(r["status"] == "success" for r in mandatory)
            complete = complete and review != "pending"
            coverage.append(
                {
                    "wallet": wallet,
                    "chain_id": cid,
                    "network": network.get("name", ""),
                    "mandatory_status": "complete" if complete else "incomplete",
                    "token_review_status": review,
                    "discovery_status": discovery_status,
                    "discovered_verification_failures": verification_failures,
                }
            )
    total_usd, priced_count, unpriced_count = _decimal_total(balances)
    summary = {
        "mandatory_total": len(checks),
        "mandatory_success": sum(r["status"] == "success" for r in checks),
        "positive_balances": len(balances),
        "priced_balance_count": priced_count,
        "unpriced_balance_count": unpriced_count,
        "priced_total_usd": total_usd,
        "coverage_complete": sum(c["mandatory_status"] == "complete" for c in coverage),
        "coverage_total": len(coverage),
    }
    payload = {
        "run": run,
        "jobs": scoped_jobs,
        "checks": checks,
        "balances": balances,
        "coverage": coverage,
        "summary": summary,
    }
    temporary: list[tuple[str, Path]] = []
    try:
        temporary.append(
            ("balances.csv", _write_csv(output, "balances.csv", balances, _CHECK_FIELDS))
        )
        temporary.append(("checks.csv", _write_csv(output, "checks.csv", checks, _CHECK_FIELDS)))
        temporary.append(
            (
                "coverage.csv",
                _write_csv(
                    output,
                    "coverage.csv",
                    coverage,
                    [
                        "wallet",
                        "chain_id",
                        "network",
                        "mandatory_status",
                        "token_review_status",
                        "discovery_status",
                        "discovered_verification_failures",
                    ],
                ),
            )
        )
        fd, path = tempfile.mkstemp(dir=output, prefix=".__tmp-", text=True)
        os.close(fd)
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
        temporary.append(("inventory.json", Path(path)))
        for name, path in temporary:
            os.replace(path, output / name)
        fd, path = tempfile.mkstemp(dir=output, prefix=".__tmp-", text=True)
        os.close(fd)
        Path(path).write_text(run_id + "\n", encoding="utf8")
        os.replace(path, output / ".inventory-run")
    finally:
        for _, path in temporary:
            if path.exists():
                path.unlink()
