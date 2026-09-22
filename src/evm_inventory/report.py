"""Atomic exports of the current wallet inventory."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

_FILES = ("balances.csv", "checks.csv", "coverage.csv", "inventory.json")
_MARKER = ".inventory-database"
_CHECK_FIELDS = [
    "wallet", "chain_id", "asset_id", "name", "symbol", "raw_balance", "decimals",
    "amount", "status", "error", "block_number", "observed_at", "price_usd",
    "price_source", "price_timestamp", "source", "verification",
]
_COVERAGE_FIELDS = [
    "wallet", "chain_id", "network", "mandatory_status", "token_review_status",
    "discovery_status", "discovered_verification_failures",
]


def _safe(value: Any) -> Any:
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text and text[:1] in "=+-@" else text


def _row(asset: dict[str, Any]) -> dict[str, Any]:
    result = asset.get("result") or {}
    meta = asset.get("metadata") or {}
    symbol = result.get("symbol") or meta.get("symbol", "")
    if asset["asset_id"] == "native" and symbol == "native":
        symbol = meta.get("symbol", "")
    raw = result.get("raw_balance")
    return {
        "wallet": asset["wallet"], "chain_id": asset["chain_id"], "asset_id": asset["asset_id"],
        "name": result.get("name") or meta.get("name", ""), "symbol": symbol,
        "raw_balance": str(raw) if raw is not None else None,
        "decimals": result.get("decimals", meta.get("decimals")), "amount": result.get("amount"),
        "status": asset["status"], "error": result.get("error"),
        "block_number": result.get("block_number"), "observed_at": result.get("observed_at"),
        "price_usd": result.get("price_usd"), "price_source": result.get("price_source"),
        "price_timestamp": result.get("price_timestamp"), "source": result.get("source"),
        "verification": result.get("verification"),
    }


def _preflight(output: Path, database_uuid: str) -> None:
    if not output.exists():
        output.mkdir(parents=True)
        return
    marker = output / _MARKER
    if marker.exists() and marker.read_text(encoding="utf-8").strip() != database_uuid:
        raise ValueError("output belongs to another database")
    allowed = set(_FILES) | {_MARKER}
    if [entry for entry in output.iterdir() if entry.name not in allowed]:
        raise ValueError("output contains unrelated files")
    if not marker.exists() and any((output / name).exists() for name in _FILES):
        raise ValueError("output is not a managed inventory export")


def _write_csv(output: Path, rows: list[dict[str, Any]], fields: list[str]) -> Path:
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


def _coverage(store, mandatory: list[dict[str, Any]], all_assets: list[dict[str, Any]]):
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for asset in mandatory:
        grouped.setdefault((asset["wallet"], asset["chain_id"]), []).append(asset)
    coverage = []
    for (wallet, chain_id), assets in grouped.items():
        metadata = assets[0]["metadata"]
        state = store.discovery_state(wallet, chain_id)
        failures = sum(
            asset["kind"] == "discovered" and asset["status"] != "success"
            for asset in all_assets
            if asset["wallet"] == wallet and asset["chain_id"] == chain_id
        )
        review = metadata.get("token_review_status", "pending")
        complete = all(asset["status"] == "success" for asset in assets) and review != "pending"
        coverage.append({
            "wallet": wallet, "chain_id": chain_id,
            "network": metadata.get("network_name", ""),
            "mandatory_status": "complete" if complete else "incomplete",
            "token_review_status": review,
            "discovery_status": state["status"] if state else "pending",
            "discovered_verification_failures": failures,
        })
    return coverage


def export_current(store, output: Path) -> None:
    """Write a safe, atomically replaced export of all retained current assets."""
    output = Path(output)
    database_uuid = store.database_uuid()
    _preflight(output, database_uuid)
    assets = store.assets()
    checks = [_row(asset) for asset in assets if asset["kind"] == "mandatory"]
    rows = [_row(asset) for asset in assets]
    balances = [
        row for row in rows
        if row["status"] in {"success", "provider_only"}
        and row["raw_balance"] is not None and int(row["raw_balance"]) > 0
    ]
    coverage = _coverage(store, [asset for asset in assets if asset["kind"] == "mandatory"], assets)
    total_usd, priced_count, unpriced_count = _decimal_total(balances)
    summary = {
        "mandatory_total": len(checks),
        "mandatory_success": sum(row["status"] == "success" for row in checks),
        "positive_balances": len(balances), "priced_balance_count": priced_count,
        "unpriced_balance_count": unpriced_count, "priced_total_usd": total_usd,
        "coverage_complete": sum(row["mandatory_status"] == "complete" for row in coverage),
        "coverage_total": len(coverage),
    }
    payload = {"database_uuid": database_uuid, "assets": assets, "checks": checks,
               "balances": balances, "coverage": coverage, "summary": summary}
    temporary: list[tuple[str, Path]] = []
    try:
        temporary.extend((
            ("balances.csv", _write_csv(output, balances, _CHECK_FIELDS)),
            ("checks.csv", _write_csv(output, checks, _CHECK_FIELDS)),
            ("coverage.csv", _write_csv(output, coverage, _COVERAGE_FIELDS)),
        ))
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
        Path(path).write_text(database_uuid + "\n", encoding="utf8")
        os.replace(path, output / _MARKER)
    finally:
        for _, path in temporary:
            if path.exists():
                path.unlink()
