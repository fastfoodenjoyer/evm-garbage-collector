from decimal import Decimal

import pytest

from evm_inventory.executor import wait_for_receipt
from evm_inventory.journal import Journal
from evm_inventory.route_execution import execute_group_entries
from evm_inventory.stages import Deadline, StageDeadlineExceeded, deadline_scope


def test_pending_receipt_yields_after_one_read():
    class Rpc:
        calls = 0

        def call(self, *args):
            self.calls += 1
            return None

    rpc = Rpc()
    with deadline_scope(Deadline(30)):
        with pytest.raises(StageDeadlineExceeded):
            wait_for_receipt(rpc, url="rpc", tx_hash="0xabc")
    assert rpc.calls == 1


def test_preflight_deadline_keeps_group_resumable(tmp_path):
    entry = {
        "wallet": "0xabc",
        "chain_id": 10,
        "asset_id": "native",
        "raw_balance": "100",
        "status": "direct_deposit",
        "valuation": {"loss_usd": "1"},
        "group": {"key": "wallet:10", "source_usd": "100", "max_route_loss_pct": "15"},
    }

    def expired(entry):
        raise StageDeadlineExceeded("stage deadline")

    with Journal(tmp_path / "state.sqlite") as journal:
        result = execute_group_entries(
            [entry],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=expired,
            submit_step=lambda *_: pytest.fail("must not submit"),
        )
        assert result["deferred"] == 1
        assert journal.group(result["group_id"])["state"] == "active"
        assert journal.group_positions(result["group_id"])[0]["state"] == "planned"


def test_pending_direct_resume_observes_hash_without_signing(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from evm_inventory.executor import signed_transaction_hash
    from evm_inventory.lifi import TransactionRequest
    from evm_inventory.route_execution import _submit_live_step

    wallet = SimpleNamespace(
        public_address="0x" + "a" * 40, bitget_deposit_address="0x" + "b" * 40, private_key="secret"
    )
    entry = {
        "wallet": wallet.public_address,
        "chain_id": 10,
        "asset_id": "native",
        "raw_balance": "100",
        "status": "direct_deposit",
        "valuation": {"loss_usd": "1"},
        "steps": [{"kind": "direct_deposit"}],
        "target": {"coin": "ETH", "chain": "Optimism", "minimum_raw": "1"},
        "group": {"key": "wallet:10", "source_usd": "100", "max_route_loss_pct": "15"},
    }
    signed = []
    raw = "0x01"
    tx_hash = signed_transaction_hash(raw)

    class Rpc:
        def call(self, url, method, params):
            if method == "eth_getTransactionReceipt":
                return None
            if method == "eth_getTransactionByHash":
                return {"hash": tx_hash}
            if method == "eth_getBalance":
                return hex(10**18)
            if method == "eth_getTransactionCount":
                return "0x1"
            if method == "eth_sendRawTransaction":
                return tx_hash
            raise AssertionError(method)

    rpc = Rpc()
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *args, **kwargs: signed.append("signature") or raw,
    )
    prepared = {
        **entry,
        "_direct_transaction": TransactionRequest(
            10, wallet.bitget_deposit_address, "0x", 100, 21000, 1
        ),
    }
    with Journal(tmp_path / "state.sqlite") as journal:

        def submit(entry, step):
            return _submit_live_step(
                entry,
                step,
                wallet=wallet,
                rpc=rpc,
                rpc_urls={10: "rpc"},
                jumper=None,
                bitget=None,
                journal=journal,
                now_ms=lambda: 0,
            )

        with deadline_scope(Deadline(30)):
            first = execute_group_entries(
                [entry],
                journal=journal,
                max_route_loss_pct=Decimal("15"),
                preflight=lambda _: prepared,
                submit_step=submit,
            )
        assert first["pending"] == 1

        def forbidden(_):
            pytest.fail("known hash must bypass source preflight")

        with deadline_scope(Deadline(30)):
            second = execute_group_entries(
                [entry],
                journal=journal,
                max_route_loss_pct=Decimal("15"),
                preflight=forbidden,
                submit_step=submit,
            )
        assert second["pending"] == 1
        assert signed == ["signature"]
        position = journal.group_positions(first["group_id"])[0]
        assert (
            journal.latest_step(position_id=position["id"], step_key="direct_deposit")["tx_hash"]
            == tx_hash
        )


def test_checkpoint_restores_prepared_types_and_excludes_secrets(tmp_path):
    from datetime import UTC, datetime

    from evm_inventory.lifi import TransactionRequest

    payload = {
        "request": TransactionRequest(10, "0x" + "a" * 40, "0x", 0, 21000, 1),
        "price": Decimal("1.25"),
        "when": datetime(2026, 1, 1, tzinfo=UTC),
        "hashes": ((10, "0xabc"),),
        "private_key": "never-save",
        "_before_sign": lambda: None,
    }
    with Journal(tmp_path / "state.sqlite") as journal:
        journal.save_checkpoint(1, "prepared", payload)
        restored = journal.checkpoint(1, "prepared")
        assert restored["request"] == payload["request"]
        assert restored["price"] == payload["price"]
        assert restored["hashes"] == payload["hashes"]
        assert "private_key" not in restored
        stored = journal.connection.execute(
            "SELECT payload_json FROM route_checkpoints"
        ).fetchone()[0]
        assert "never-save" not in stored


def test_bridge_pending_yields_without_sleep(tmp_path):
    from evm_inventory.models import AssetIdentity
    from evm_inventory.route_execution import _await_cross_chain_transfer

    tx_hash = "0x" + "a" * 64

    class Jumper:
        calls = 0

        def transaction_status(self, **kwargs):
            self.calls += 1
            return {"status": "PENDING", "sending": {"txHash": tx_hash}}

    jumper = Jumper()
    with Journal(tmp_path / "state.sqlite") as journal:
        position = journal.get_or_create_position(position_key="bridge", wallet="wallet")
        step = journal.record_step_intent(
            position_id=position["id"],
            step_key="bridge",
            nonce=1,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
        )
        journal.record_broadcast_attempt(step["id"], tx_hash)
        with deadline_scope(Deadline(30)):
            with pytest.raises(StageDeadlineExceeded, match="bridge_pending"):
                _await_cross_chain_transfer(
                    jumper=jumper,
                    rpc=None,
                    rpc_urls={},
                    journal=journal,
                    position_id=position["id"],
                    step_key="bridge",
                    tx_hash=tx_hash,
                    bridge="bridge",
                    source=AssetIdentity(10, "native", 18),
                    destination=AssetIdentity(8453, "native", 18),
                    sender="wallet",
                    recipient="wallet",
                    baseline_raw=0,
                    sleep=lambda _: pytest.fail("must yield without sleeping"),
                )
        assert journal.step(step["id"])["state"] == "awaiting_bridge"
        assert jumper.calls == 1


def test_swap_receipt_resume_retains_original_native_baseline(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from evm_inventory.lifi import TransactionRequest, _route_from_dict
    from evm_inventory.live_plan import _route_data
    from evm_inventory.route_execution import _submit_live_step

    sender = "0x" + "a" * 40
    token = "0x" + "b" * 40
    timestamp = "2026-01-01T00:00:00Z"
    route = _route_from_dict(
        {
            "id": "swap",
            "fromAmount": "1000",
            "toAmount": "50",
            "toAmountMin": "49",
            "steps": [
                {
                    "id": "swap-step",
                    "type": "swap",
                    "tool": "swap",
                    "action": {
                        "fromChainId": 10,
                        "toChainId": 10,
                        "fromToken": {"address": token, "decimals": 0},
                        "toToken": {"address": "0x" + "0" * 40, "decimals": 18},
                        "fromAmount": "1000",
                        "toAddress": sender,
                    },
                    "estimate": {"toAmount": "50"},
                }
            ],
        },
        quote_timestamp=timestamp,
    )
    step = {
        "kind": "swap_to_native",
        "route": _route_data(route),
        "_live_route": route,
        "_prepared_requests": (TransactionRequest(10, token, "0x", 0, 21000, 1),),
    }
    candidate = SimpleNamespace(loss_usd=Decimal("1"))
    wallet = SimpleNamespace(public_address=sender, bitget_deposit_address=sender)
    stage = [0]
    refreshed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution._refresh_route_step",
        lambda *args, **kwargs: refreshed.append("fresh") or (candidate, step),
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution._asset_balance",
        lambda rpc, **kwargs: (
            (100 if stage[0] == 0 else 150) if kwargs["asset"].is_native else 1000
        ),
    )
    tx_hash = "0x" + "c" * 64

    class Rpc:
        def call(self, url, method, params):
            assert method == "eth_getTransactionReceipt"
            return {"gasUsed": "0xa", "effectiveGasPrice": "0x1"}

    with Journal(tmp_path / "state.sqlite") as journal:
        position = journal.get_or_create_position(position_key="swap", wallet=sender)
        entry = {
            "_journal_position_id": position["id"],
            "steps": [step],
            "_candidate": candidate,
            "_step_candidates": {"swap": candidate},
        }

        def execute(**kwargs):
            if stage[0] == 0:
                intent = journal.record_step_intent(
                    position_id=position["id"],
                    step_key=kwargs["entry"]["_journal_step_key"],
                    nonce=1,
                    calldata_digest="d" * 64,
                    signed_payload_digest="e" * 64,
                )
                journal.record_broadcast_attempt(intent["id"], tx_hash)
                raise StageDeadlineExceeded("receipt_pending")
            return tx_hash

        monkeypatch.setattr("evm_inventory.route_execution._execute_entry", execute)
        with deadline_scope(Deadline(30)), pytest.raises(StageDeadlineExceeded):
            _submit_live_step(
                entry,
                step,
                wallet=wallet,
                rpc=Rpc(),
                rpc_urls={10: "rpc"},
                jumper=None,
                bitget=None,
                journal=journal,
                now_ms=lambda: 0,
            )
        stage[0] = 1
        with deadline_scope(Deadline(30)):
            result = _submit_live_step(
                entry,
                step,
                wallet=wallet,
                rpc=Rpc(),
                rpc_urls={10: "rpc"},
                jumper=None,
                bitget=None,
                journal=journal,
                now_ms=lambda: 0,
            )
        assert result["actual_balance_raw"] == "50"
        assert result["generated_output_raw"] == "60"
        assert refreshed == ["fresh"]
