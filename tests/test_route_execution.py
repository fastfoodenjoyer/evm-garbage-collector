from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.journal import Journal
from evm_inventory.lifi import (
    LifiPriceEvidence,
    TransactionRequest,
    _route_from_dict,
)
from evm_inventory.live_plan import _route_data
from evm_inventory.models import AssetIdentity
from evm_inventory.planner_gas import PlannerGasEstimator
from evm_inventory.route_execution import (
    _execute_entry,
    _preflight_live_entry,
    _submit_live_step,
    execute_entries,
    execute_group_entries,
    revalidate_route_evidence,
)
from evm_inventory.workbook import WalletWorkbookRow

WALLET = "0x" + "1" * 40
ASSET_A = "0x" + "a" * 40
ASSET_B = "0x" + "b" * 40
DEPOSIT = "0x" + "d" * 40
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def _entry(asset_id, *, status="direct_deposit", loss="1", steps=None):
    return {
        "wallet": WALLET,
        "chain_id": 10,
        "asset_id": asset_id,
        "raw_balance": "1000000",
        "decimals": 6,
        "status": status,
        "deposit_address": DEPOSIT,
        "target": {
            "coin": "USDC",
            "chain_id": 8453,
            "chain": "Base",
            "asset_id": "0x" + "e" * 40,
            "minimum_raw": "1",
            "decimals": 6,
        },
        "steps": steps if steps is not None else [{"kind": "direct_deposit"}],
        "valuation": {"loss_usd": loss, "loss_pct": loss},
        "group": {
            "key": f"{WALLET}:10",
            "wallet": WALLET,
            "source_chain_id": 10,
            "source_usd": "100",
            "loss_usd": "3",
            "loss_pct": "3",
            "max_route_loss_pct": "15",
        },
    }


def test_group_rechecks_changed_costs_before_signing_later_siblings(tmp_path):
    entries = [
        _entry(ASSET_A, status="direct_deposit", loss="1"),
        _entry(ASSET_B, status="route_ready", loss="1", steps=[{"kind": "bridge"}]),
        _entry("0x" + "c" * 40, status="direct_deposit", loss="1"),
    ]
    submitted = []

    def preflight(entry):
        fresh = {**entry, "valuation": dict(entry["valuation"])}
        if entry["asset_id"] == ASSET_B:
            fresh["valuation"]["loss_usd"] = "14"
        return fresh

    def submit(entry, _step):
        submitted.append(entry["asset_id"])
        return {"realized_loss_usd": "1", "state": "completed"}

    with Journal(tmp_path / "journal.sqlite") as journal:
        result = execute_group_entries(
            entries,
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=preflight,
            submit_step=submit,
        )
        group = journal.group(result["group_id"])
        positions = journal.connection.execute(
            "SELECT state FROM route_positions ORDER BY id"
        ).fetchall()

    assert submitted == [ASSET_A]
    assert result["manual_review_group_threshold_exceeded"] == 2
    assert group["state"] == "manual_review_group_threshold_exceeded"
    assert [row["state"] for row in positions] == [
        "completed",
        "manual_review_group_threshold_exceeded",
        "manual_review_group_threshold_exceeded",
    ]


def test_failed_post_swap_requote_records_actual_asset_then_halts_siblings(tmp_path):
    bridge = {
        "kind": "bridge",
        "route": {
            "id": "planned-bridge",
            "evidence": {"payload_sha256": "a" * 64},
        },
    }
    swap = {
        "kind": "swap_to_native",
        "route": {"id": "source-swap", "evidence": {"payload_sha256": "c" * 64}},
    }
    entries = [
        _entry(ASSET_A, status="route_ready", loss="1", steps=[swap, bridge]),
        _entry(ASSET_B, status="direct_deposit", loss="1"),
    ]
    submitted_steps = []

    def submit(entry, step):
        submitted_steps.append((entry["asset_id"], step["kind"]))
        return {
            "state": "confirmed",
            "actual_asset_id": "native",
            "actual_balance_raw": "70000000000000000",
            "realized_loss_usd": "2",
        }

    def failed_requote(_entry, _balance):
        raise ValueError("no eligible bridge route")

    with Journal(tmp_path / "journal.sqlite") as journal:
        result = execute_group_entries(
            entries,
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda entry: entry,
            submit_step=submit,
            requote_after_swap=failed_requote,
        )
        positions = journal.connection.execute(
            "SELECT * FROM route_positions ORDER BY id"
        ).fetchall()

    assert submitted_steps == [(ASSET_A, "swap_to_native")]
    assert result["manual_review_after_swap"] == 1
    assert result["manual_review_group_halted"] == 1
    assert positions[0]["state"] == "manual_review_after_swap"
    assert positions[0]["actual_asset_id"] == "native"
    assert positions[0]["actual_balance_raw"] == "70000000000000000"
    assert positions[0]["reason"] == "no eligible bridge route"
    assert positions[1]["state"] == "manual_review_group_halted"


def test_group_preflight_failure_does_not_call_submitter(tmp_path):
    entry = _entry(ASSET_A)
    submitted = []

    def fail_preflight(_entry):
        raise ValueError("route payload changed")

    with Journal(tmp_path / "journal.sqlite") as journal:
        result = execute_group_entries(
            [entry],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=fail_preflight,
            submit_step=lambda *_args: submitted.append(True),
        )

    assert submitted == []
    assert result["manual_review"] == 1


def test_group_execution_rejects_loss_limit_changed_after_plan(tmp_path):
    entry = _entry(ASSET_A)
    submitted = []
    with Journal(tmp_path / "journal.sqlite") as journal:
        with pytest.raises(ValueError, match="differs from the approved plan"):
            execute_group_entries(
                [entry],
                journal=journal,
                max_route_loss_pct=Decimal("20"),
                preflight=lambda planned: planned,
                submit_step=lambda *_args: submitted.append(True),
            )

    assert submitted == []


def test_execute_entries_uses_group_preflight_dependencies_before_submission(tmp_path):
    entry = _entry(ASSET_A)
    wallet = WalletWorkbookRow(2, 1, WALLET, "0x" + "1" * 64, DEPOSIT)
    submitted = []

    result = execute_entries(
        [entry],
        wallets={WALLET: wallet},
        rpc_urls={},
        journal_path=tmp_path / "journal.sqlite",
        execute=True,
        delay_min_seconds=0,
        delay_max_seconds=0,
        max_route_loss_pct=Decimal("15"),
        preflight_entry=lambda planned: planned,
        submit_route_step=lambda fresh, step: submitted.append(step["kind"])
        or {"state": "completed", "realized_loss_usd": "1"},
    )

    assert submitted == ["direct_deposit"]
    assert result["completed"] == 1


def test_route_revalidation_rejects_changed_identity_amount_recipient_and_hash():
    source = AssetIdentity(10, ASSET_A, 6)
    destination = AssetIdentity(8453, "0x" + "e" * 40, 6)
    planned = _lifi_route(route_id="saved", output_amount=900_000)

    assert revalidate_route_evidence(
        planned,
        planned,
        expected_source=source,
        expected_destination=destination,
        expected_input_amount=1_000_000,
        recipient=DEPOSIT,
    ) is planned

    for current, expected_input, recipient in (
        (_lifi_route(route_id="changed-id", output_amount=900_000), 1_000_000, DEPOSIT),
        (_lifi_route(route_id="saved", output_amount=850_000), 1_000_000, DEPOSIT),
        (
            _lifi_route(route_id="saved", output_amount=900_000, source=ASSET_B),
            1_000_000,
            DEPOSIT,
        ),
        (
            _lifi_route(
                route_id="saved", output_amount=900_000, destination="0x" + "f" * 40
            ),
            1_000_000,
            DEPOSIT,
        ),
        (planned, 999_999, DEPOSIT),
        (planned, 1_000_000, WALLET),
    ):
        with pytest.raises(ValueError, match="requote_required"):
            revalidate_route_evidence(
                planned,
                current,
                expected_source=source,
                expected_destination=destination,
                expected_input_amount=expected_input,
                recipient=recipient,
            )


def test_generated_native_gas_reserve_cannot_use_preexisting_wallet_balance(
    tmp_path, monkeypatch
):
    wallet = WalletWorkbookRow(2, 1, WALLET, "0x" + "1" * 64, DEPOSIT)
    request = TransactionRequest(10, DEPOSIT, "0x", 98, 21_000, 1)
    signed = []

    class Rpc:
        def call(self, _url, method, _params):
            if method == "eth_getBalance":
                return hex(10_000)
            raise AssertionError(method)

    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *_args, **_kwargs: signed.append(True) or "0x01",
    )
    with Journal(tmp_path / "journal.sqlite") as journal:
        position = journal.get_or_create_position(
            position_key="generated-native", wallet=WALLET
        )
        with pytest.raises(ValueError, match="insufficient native balance for gas reserve"):
            _execute_entry(
                entry={
                    "chain_id": 10,
                    "asset_id": "native",
                    "status": "route_ready",
                    "_actual_balance_raw": "100",
                    "route": {"step": {}},
                },
                wallet=wallet,
                rpc=Rpc(),
                jumper=object(),
                rpc_urls={10: "https://rpc.example"},
                journal=journal,
                position_id=position["id"],
                prepared_request=request,
            )

    assert signed == []


@pytest.mark.parametrize("intermediate_output", [900, 800])
def test_submit_executes_every_normalized_lifi_step_with_actual_intermediate_balance(
    tmp_path, monkeypatch, intermediate_output
):
    route = _multi_step_route()
    wallet = WalletWorkbookRow(2, 1, WALLET, "0x" + "1" * 64, DEPOSIT)
    balances = {ASSET_A: 1_000, ASSET_B: 0, "0x" + "e" * 40: 0}
    submitted = []

    class Rpc:
        def call(self, _url, method, params):
            assert method == "eth_call"
            token = params[0]["to"]
            return "0x" + hex(balances[token])[2:].rjust(64, "0")

    def fake_execute_entry(*, entry, prepared_request, **_kwargs):
        submitted.append((entry["asset_id"], prepared_request.to))
        if entry["asset_id"] == ASSET_A:
            balances[ASSET_A] = 0
            balances[ASSET_B] = intermediate_output
        else:
            balances[ASSET_B] = 0
        return f"0x{len(submitted):064x}"

    monkeypatch.setattr("evm_inventory.route_execution._execute_entry", fake_execute_entry)
    prepared = (
        TransactionRequest(10, "0x" + "1" * 40, "0x1234", 0, 50_000, 1),
        TransactionRequest(10, "0x" + "2" * 40, "0x5678", 0, 50_000, 1),
    )
    step = {
        "kind": "bridge",
        "route": _route_data(route),
        "_live_route": route,
        "_prepared_requests": prepared,
    }
    entry = {
        "status": "route_ready",
        "chain_id": 10,
        "asset_id": ASSET_A,
        "raw_balance": "1000",
        "steps": [step],
        "_journal_position_id": 1,
        "_candidate": SimpleNamespace(loss_usd=Decimal("1")),
        "_step_candidates": {route.route_id: SimpleNamespace(loss_usd=Decimal("1"))},
    }

    with Journal(tmp_path / "journal.sqlite") as journal:
        position = journal.get_or_create_position(
            position_key="wallet:10:multi", wallet=WALLET
        )
        entry["_journal_position_id"] = position["id"]
        if intermediate_output < 900:
            with pytest.raises(
                ValueError, match="requote_required:insufficient_intermediate_balance"
            ):
                _submit_live_step(
                    entry,
                    step,
                    wallet=wallet,
                    rpc=Rpc(),
                    rpc_urls={10: "https://rpc.example", 8453: "https://base.example"},
                    jumper=object(),
                    bitget=object(),
                    journal=journal,
                    now_ms=lambda: int(NOW.timestamp() * 1000),
                )
            result = None
        else:
            result = _submit_live_step(
                entry,
                step,
                wallet=wallet,
                rpc=Rpc(),
                rpc_urls={10: "https://rpc.example", 8453: "https://base.example"},
                jumper=object(),
                bitget=object(),
                journal=journal,
                now_ms=lambda: int(NOW.timestamp() * 1000),
            )
        saved_position = journal.position(position["id"])

    if intermediate_output < 900:
        assert result is None
        assert submitted == [(ASSET_A, prepared[0].to)]
    else:
        assert result == {"state": "confirmed", "realized_loss_usd": "1"}
        assert submitted == [(ASSET_A, prepared[0].to), (ASSET_B, prepared[1].to)]
    assert saved_position["actual_asset_id"] == ASSET_B
    assert saved_position["actual_balance_raw"] == str(intermediate_output)


def _multi_step_route():
    timestamp = NOW.isoformat()
    return _route_from_dict(
        {
            "id": "two-local-steps",
            "fromAmount": "1000",
            "toAmount": "800",
            "toAmountMin": "790",
            "priceTimestamp": timestamp,
            "steps": [
                {
                    "id": "swap",
                    "type": "swap",
                    "tool": "swap-provider",
                    "action": {
                        "fromChainId": 10,
                        "toChainId": 10,
                        "fromToken": {"address": ASSET_A, "decimals": 6},
                        "toToken": {"address": ASSET_B, "decimals": 6},
                        "fromAmount": "1000",
                        "toAddress": WALLET,
                    },
                    "estimate": {"toAmount": "900"},
                },
                {
                    "id": "bridge",
                    "type": "cross",
                    "tool": "bridge-provider",
                    "action": {
                        "fromChainId": 10,
                        "toChainId": 8453,
                        "fromToken": {"address": ASSET_B, "decimals": 6},
                        "toToken": {"address": "0x" + "e" * 40, "decimals": 6},
                        "fromAmount": "900",
                        "toAddress": DEPOSIT,
                    },
                    "estimate": {"toAmount": "800"},
                },
            ],
        },
        quote_timestamp=timestamp,
    )


def test_direct_preflight_refreshes_balance_target_and_gas_without_route_quote():
    asset = AssetIdentity(10, ASSET_A, 6)
    target = BitgetDepositTarget("USDC", 10, ASSET_A, 1, "Optimism", 6)
    wallet = WalletWorkbookRow(2, 1, WALLET, "0x" + "1" * 64, DEPOSIT)

    class Rpc:
        calls = []

        def call(self, _url, method, params):
            self.calls.append((method, params))
            if method == "eth_call":
                return "0x" + hex(2_000_000)[2:].rjust(64, "0")
            if method == "eth_gasPrice":
                return "0x3b9aca00"
            if method == "eth_estimateGas":
                return "0x5208"
            raise AssertionError(method)

    class Prices:
        route_requests = 0

        def token_price(self, requested):
            price = "1" if not requested.is_native else "2000"
            return LifiPriceEvidence(requested, price, NOW.isoformat())

        def routes(self, _request):
            self.route_requests += 1
            raise AssertionError("direct deposit must not request a LI.FI route")

    class Bitget:
        def revalidate_deposit_target(self, _target, *, recipient):
            assert recipient == DEPOSIT
            return target

    rpc = Rpc()
    prices = Prices()
    entry = _entry(ASSET_A)
    entry.update(
        {
            "chain_id": asset.chain_id,
            "asset_id": asset.contract_address,
            "decimals": asset.decimals,
            "target": {
                "coin": target.coin,
                "chain_id": target.chain_id,
                "chain": target.chain,
                "asset_id": target.asset_id,
                "minimum_raw": str(target.minimum_raw),
                "decimals": target.decimals,
            },
        }
    )

    fresh = _preflight_live_entry(
        entry,
        wallet=wallet,
        rpc=rpc,
        rpc_urls={10: "https://rpc.example"},
        jumper=prices,
        bitget=Bitget(),
        gas_estimator=PlannerGasEstimator(
            rpc, {10: "https://rpc.example"}, prices
        ),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )

    assert fresh["raw_balance"] == "2000000"
    assert fresh["steps"][0]["kind"] == "direct_deposit"
    assert fresh["_direct_transaction"].data.startswith("0xa9059cbb")
    assert fresh["_candidate"].is_valid
    assert prices.route_requests == 0


def test_direct_preflight_rejects_changed_live_target_minimum_before_rpc():
    target = BitgetDepositTarget("USDC", 10, ASSET_A, 2, "Optimism", 6)
    wallet = WalletWorkbookRow(2, 1, WALLET, "0x" + "1" * 64, DEPOSIT)

    class Rpc:
        def call(self, *_args):
            raise AssertionError("changed target must be rejected before RPC")

    class Bitget:
        def revalidate_deposit_target(self, _target, *, recipient):
            assert recipient == DEPOSIT
            return target

    entry = _entry(ASSET_A)
    entry["target"].update({"chain_id": 10, "asset_id": ASSET_A, "chain": "Optimism"})

    with pytest.raises(ValueError, match="requote_required:target_minimum_changed"):
        _preflight_live_entry(
            entry,
            wallet=wallet,
            rpc=Rpc(),
            rpc_urls={10: "https://rpc.example"},
            jumper=object(),
            bitget=Bitget(),
            gas_estimator=object(),
            now_ms=lambda: int(NOW.timestamp() * 1000),
        )


def _lifi_route(
    *, route_id, output_amount, source=ASSET_A, destination="0x" + "e" * 40
):
    timestamp = "2026-09-23T12:00:00Z"
    return _route_from_dict(
        {
            "id": route_id,
            "fromAmount": "1000000",
            "toAmount": str(output_amount),
            "toAmountMin": str(output_amount - 1),
            "priceTimestamp": timestamp,
            "fromTokenPriceUSD": "1",
            "toTokenPriceUSD": "1",
            "gasCosts": [
                {
                    "amount": "100",
                    "priceUSD": "2000",
                    "token": {
                        "chainId": 10,
                        "address": "0x0000000000000000000000000000000000000000",
                        "decimals": 18,
                    },
                    "timestamp": timestamp,
                }
            ],
            "steps": [
                {
                    "id": "step",
                    "type": "cross",
                    "tool": "provider",
                    "action": {
                        "fromChainId": 10,
                        "toChainId": 8453,
                        "fromToken": {"address": source, "decimals": 6},
                        "toToken": {"address": destination, "decimals": 6},
                        "fromAmount": "1000000",
                        "toAddress": DEPOSIT,
                    },
                    "estimate": {"toAmount": str(output_amount)},
                }
            ],
        },
        quote_timestamp=timestamp,
    )
