import time

import pytest
from eth_account import Account

from evm_inventory.executor import (
    AmbiguousBroadcast,
    EthereumGasDeferred,
    broadcast_durable_transaction,
    require_ethereum_gas_below_limit,
    require_native_reserve,
    sign_transaction,
    signed_transaction_hash,
)
from evm_inventory.journal import Journal
from evm_inventory.lifi import TransactionRequest
from evm_inventory.route_execution import (
    _approve_if_needed,
    _direct_request,
    _execute_entry,
    advance_route_state,
    execute_entries,
    reconcile_bridge_observation,
    requote_after_swap_output,
)
from evm_inventory.rpc import RpcError
from evm_inventory.workbook import WalletWorkbookRow


def test_sign_transaction_uses_expected_private_key_and_chain():
    key = "0x" + "1" * 64
    sender = Account.from_key(key).address
    request = TransactionRequest(
        chain_id=10,
        to="0x" + "2" * 40,
        data="0x",
        value=0,
        gas_limit=21_000,
        gas_price_wei=1_000_000,
    )

    raw = sign_transaction(request, private_key=key, expected_sender=sender, nonce=4)

    assert raw.startswith("0x")


def test_sign_transaction_accepts_normalized_contract_address_with_letters():
    key = "0x" + "1" * 64
    sender = Account.from_key(key).address
    request = TransactionRequest(
        chain_id=56,
        to="0xd4888870c8686c748232719051b677791dbda26d",
        data="0x3ccfd60b",
        value=0,
        gas_limit=100_000,
        gas_price_wei=100_000_000,
    )

    raw = sign_transaction(request, private_key=key, expected_sender=sender, nonce=0)

    assert raw.startswith("0x")


def test_sign_transaction_rejects_wrong_sender():
    request = TransactionRequest(10, "0x" + "2" * 40, "0x", 0, 21_000, 1)

    with pytest.raises(ValueError, match="private key"):
        sign_transaction(
            request,
            private_key="0x" + "1" * 64,
            expected_sender="0x" + "3" * 40,
            nonce=0,
        )


def test_native_reserve_defaults_to_three_times_estimated_gas():
    assert require_native_reserve(balance=30, gas_cost=10) == 30
    with pytest.raises(ValueError, match="gas reserve"):
        require_native_reserve(balance=29, gas_cost=10)


def test_native_reserve_accepts_explicit_multiplier_override():
    assert require_native_reserve(balance=100, gas_cost=10, multiplier=5) == 50
    with pytest.raises(ValueError, match="gas reserve"):
        require_native_reserve(balance=49, gas_cost=10, multiplier=5)


def test_batch_executor_refuses_without_explicit_execute(tmp_path):
    with pytest.raises(ValueError, match="--execute"):
        execute_entries(
            [],
            wallets={},
            rpc_urls={},
            journal_path=tmp_path / "journal.sqlite",
            execute=False,
        )


def test_ambiguous_broadcast_recovers_recorded_hash_without_signing_again(tmp_path):
    tx_hash = "0x" + "a" * 64

    class Rpc:
        calls = []

        def call(self, _url, method, _params):
            self.calls.append(method)
            if method == "eth_sendRawTransaction":
                raise TimeoutError("rpc timeout after broadcast")
            if method == "eth_getTransactionByHash":
                return {"hash": tx_hash, "nonce": "0x7"}
            raise AssertionError(method)

    rpc = Rpc()
    with Journal(tmp_path / "journal.sqlite") as journal:
        position = journal.get_or_create_position(
            position_key="wallet:1:asset", wallet="0x" + "1" * 40
        )
        first = broadcast_durable_transaction(
            rpc,
            url="https://rpc",
            journal=journal,
            position_id=position["id"],
            step_key="bridge",
            nonce=7,
            calldata="0x1234",
            signed_transaction="0x01",
            tx_hash=tx_hash,
        )
        recovered = broadcast_durable_transaction(
            rpc,
            url="https://rpc",
            journal=journal,
            position_id=position["id"],
            step_key="bridge",
            nonce=7,
            calldata="0x1234",
            signed_transaction="0x01",
            tx_hash=tx_hash,
        )

    assert first is None
    assert recovered == tx_hash
    assert rpc.calls == ["eth_sendRawTransaction", "eth_getTransactionByHash"]


def test_ambiguous_approval_uses_durable_step_and_does_not_fallback_broadcast(
    tmp_path, monkeypatch
):
    wallet = _wallet()
    route_step_key = "bridge:route-1"
    durable_calls = []
    fallback_calls = []
    monkeypatch.setattr(
        "evm_inventory.route_execution.token_allowance", lambda *_args, **_kwargs: 0
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution._native_balance", lambda *_args, **_kwargs: 10**18
    )
    monkeypatch.setattr("evm_inventory.route_execution.pending_nonce", lambda *_a, **_k: 7)
    monkeypatch.setattr(
        "evm_inventory.route_execution.require_ethereum_gas_below_limit",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *_args, **_kwargs: "0x01",
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.signed_transaction_hash",
        lambda _raw: "0x" + "a" * 64,
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.broadcast_signed_transaction",
        lambda *_args, **_kwargs: fallback_calls.append(True),
    )

    def ambiguous_broadcast(_rpc, **kwargs):
        durable_calls.append(kwargs["step_key"])
        return None

    monkeypatch.setattr(
        "evm_inventory.route_execution.broadcast_durable_transaction",
        ambiguous_broadcast,
    )
    request = TransactionRequest(10, "0x" + "4" * 40, "0x", 0, 50_000, 1)
    with Journal(tmp_path / "journal.sqlite") as journal:
        position = journal.get_or_create_position(
            position_key="wallet:10:approval", wallet=wallet.public_address
        )
        with pytest.raises(AmbiguousBroadcast):
            _approve_if_needed(
                {
                    "asset_id": "0x" + "3" * 40,
                    "raw_balance": "1000",
                    "route": {"step": {"estimate": {"approvalAddress": "0x" + "5" * 40}}},
                },
                request,
                wallet=wallet,
                rpc=object(),
                url="https://rpc.example",
                journal=journal,
                position_id=position["id"],
                route_step_key=route_step_key,
            )

    assert durable_calls == [f"approval:{route_step_key}"]
    assert fallback_calls == []


def test_failed_swap_cannot_transition_to_bridge_submission():
    assert advance_route_state("submitted", "swap_reverted") == "manual_review"
    with pytest.raises(ValueError, match="invalid route transition"):
        advance_route_state("manual_review", "submit_bridge")


def test_non_stable_swap_output_is_requoted_under_original_loss_cap():
    entry = {"reservations": {"whole_position_loss_usd": "2.50"}}
    seen = []

    route = requote_after_swap_output(
        entry,
        observed_output_raw=987,
        requote=lambda amount: seen.append(amount) or {"valuation": {"loss_usd": "2.49"}},
    )

    assert seen == [987]
    assert route["valuation"]["loss_usd"] == "2.49"

    with pytest.raises(ValueError, match="loss cap"):
        requote_after_swap_output(
            entry, observed_output_raw=987,
            requote=lambda _: {"valuation": {"loss_usd": "2.51"}},
        )


def test_bridge_reconciliation_requires_correlation_and_reports_at_30_minutes(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        position = journal.get_or_create_position(
            position_key="wallet:1:asset", wallet="0x" + "1" * 40
        )
        step = journal.record_step_intent(
            position_id=position["id"], step_key="bridge", nonce=7,
            calldata_digest="a" * 64, signed_payload_digest="b" * 64,
        )
        journal.record_broadcast_attempt(step["id"], "0x" + "c" * 64)
        journal.record_receipt(step["id"], status="confirmed", finality_block=123)
        journal.record_balance_baseline(step["id"], balance_raw=100, expected_delta_raw=90)

        early = reconcile_bridge_observation(
            journal, step_id=step["id"], observed_balance_raw=500,
            correlated_arrival_raw=None, confirmed_at_ms=0, now_ms=1_799_999,
        )
        timeout = reconcile_bridge_observation(
            journal, step_id=step["id"], observed_balance_raw=500,
            correlated_arrival_raw=None, confirmed_at_ms=0, now_ms=1_800_000,
        )
        credited = reconcile_bridge_observation(
            journal, step_id=step["id"], observed_balance_raw=190,
            correlated_arrival_raw=89, confirmed_at_ms=0, now_ms=1_800_000,
        )

        row = journal.step(step["id"])

    assert early == "awaiting_bridge"
    assert timeout == "no_correlated_arrival"
    assert credited == "credited"
    assert row["state"] == "credited"


class _Rpc:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, url, method, params):
        self.calls.append(method)
        response = self.responses.pop(0)
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return response


def test_native_direct_request_retains_three_gas_reserves():
    wallet = _wallet()
    gas_price = 2
    balance = 1_000_000
    rpc = _Rpc([hex(gas_price), hex(balance)])
    entry = {**_direct_entry(wallet.public_address), "target": {"minimum_raw": 874_000}}

    request = _direct_request(entry, wallet=wallet, rpc=rpc, url="https://rpc")

    assert request.value == balance - 3 * 21_000 * gas_price


def test_native_direct_request_rejects_amount_below_minimum_after_three_gas_reserves():
    wallet = _wallet()
    gas_price = 2
    balance = 1_000_000
    rpc = _Rpc([hex(gas_price), hex(balance)])
    entry = {**_direct_entry(wallet.public_address), "target": {"minimum_raw": 874_001}}

    with pytest.raises(ValueError, match="native balance is below Bitget minimum"):
        _direct_request(entry, wallet=wallet, rpc=rpc, url="https://rpc")


@pytest.mark.parametrize(
    ("gas_price", "reason"),
    [
        (
            "0x1dcd6500",
            "ethereum_gas_deferred:source=rpc;value_wei=500000000;threshold_wei=500000000",
        ),
        ("not-a-quantity", "ethereum_gas_deferred:source=rpc_probe_error;threshold_wei=500000000"),
        ("-1", "ethereum_gas_deferred:source=rpc_probe_error;threshold_wei=500000000"),
        (
            RpcError("rpc_error"),
            "ethereum_gas_deferred:source=rpc_probe_error;threshold_wei=500000000",
        ),
    ],
)
def test_ethereum_guard_defers_at_threshold_or_invalid_rpc_gas(gas_price, reason):
    rpc = _Rpc(["0x1", gas_price])
    request = TransactionRequest(1, "0x" + "2" * 40, "0x", 0, 21_000, 1)

    with pytest.raises(EthereumGasDeferred, match=reason) as exc_info:
        require_ethereum_gas_below_limit(rpc, url="https://rpc", request=request)

    assert exc_info.value.reason == reason
    assert rpc.calls == ["eth_chainId", "eth_gasPrice"]


def test_ethereum_guard_defers_at_planned_price_threshold_after_fresh_probe():
    rpc = _Rpc(["0x1", "0x1"])
    request = TransactionRequest(1, "0x" + "2" * 40, "0x", 0, 21_000, 500_000_000)

    with pytest.raises(EthereumGasDeferred) as exc_info:
        require_ethereum_gas_below_limit(rpc, url="https://rpc", request=request)

    assert exc_info.value.reason == (
        "ethereum_gas_deferred:source=plan;value_wei=500000000;threshold_wei=500000000"
    )
    assert rpc.calls == ["eth_chainId", "eth_gasPrice"]


def test_ethereum_guard_does_not_probe_non_ethereum_requests():
    rpc = _Rpc([])
    request = TransactionRequest(56, "0x" + "2" * 40, "0x", 0, 21_000, 500_000_000)

    require_ethereum_gas_below_limit(rpc, url="https://rpc", request=request)

    assert rpc.calls == []


def test_direct_deposit_is_guarded_before_signing(monkeypatch):
    wallet = _wallet()
    rpc = _Rpc(["0x1", "0x100000", "0x100000", "0x0", "0x1", "0x1dcd6500"])
    signed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *args, **kwargs: signed.append(args),
    )

    with pytest.raises(EthereumGasDeferred):
        _execute_entry(
            entry=_direct_entry(wallet.public_address),
            wallet=wallet,
            rpc=rpc,
            jumper=object(),
            rpc_urls={1: "https://rpc"},
        )

    assert signed == []
    assert "eth_sendRawTransaction" not in rpc.calls
    assert rpc.calls[-2:] == ["eth_chainId", "eth_gasPrice"]


@pytest.mark.parametrize("chain_id", ["not-a-quantity", "0x2"])
def test_direct_deposit_rejects_bad_ethereum_chain_identity_before_signing(monkeypatch, chain_id):
    wallet = _wallet()
    rpc = _Rpc(["0x1", "0x100000", "0x100000", "0x0", chain_id])
    signed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *args, **kwargs: signed.append(args),
    )

    with pytest.raises(EthereumGasDeferred):
        _execute_entry(
            entry=_direct_entry(wallet.public_address),
            wallet=wallet,
            rpc=rpc,
            jumper=object(),
            rpc_urls={1: "https://rpc"},
        )

    assert signed == []
    assert "eth_sendRawTransaction" not in rpc.calls


@pytest.mark.parametrize("gas_price", ["invalid", -1])
def test_invalid_ethereum_planned_gas_price_defers_before_signing(monkeypatch, gas_price):
    wallet = _wallet()
    request = TransactionRequest(1, "0x" + "4" * 40, "0x", 0, 21_000, gas_price)
    signed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution._native_balance", lambda *args, **kwargs: 1_000_000
    )
    monkeypatch.setattr("evm_inventory.route_execution.pending_nonce", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *args, **kwargs: signed.append(args),
    )
    jumper = type("Jumper", (), {"step_transaction": lambda self, step: request})()
    rpc = _Rpc([])

    with pytest.raises(EthereumGasDeferred):
        _execute_entry(
            entry={**_route_entry(wallet.public_address), "asset_id": "native"},
            wallet=wallet,
            rpc=rpc,
            jumper=jumper,
            rpc_urls={1: "https://rpc"},
        )

    assert signed == []
    assert "eth_sendRawTransaction" not in rpc.calls


@pytest.mark.parametrize("approval_gas_price", ["invalid", -1])
def test_invalid_approval_gas_price_defers_before_signing(monkeypatch, approval_gas_price):
    wallet = _wallet()
    request = TransactionRequest(1, "0x" + "4" * 40, "0x", 0, 21_000, 1)
    signed = []
    monkeypatch.setattr("evm_inventory.route_execution.token_allowance", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution._native_balance", lambda *args, **kwargs: 1_000_000
    )
    monkeypatch.setattr("evm_inventory.route_execution.pending_nonce", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution.approve_transaction",
        lambda **kwargs: TransactionRequest(
            1, "0x" + "3" * 40, "0x", 0, 100_000, approval_gas_price
        ),
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *args, **kwargs: signed.append(args),
    )
    jumper = type("Jumper", (), {"step_transaction": lambda self, step: request})()
    rpc = _Rpc([])

    with pytest.raises(EthereumGasDeferred):
        _execute_entry(
            entry=_route_entry(wallet.public_address),
            wallet=wallet,
            rpc=rpc,
            jumper=jumper,
            rpc_urls={1: "https://rpc"},
        )

    assert signed == []
    assert "eth_sendRawTransaction" not in rpc.calls


def test_approval_gas_deferral_does_not_sign_or_broadcast(monkeypatch):
    wallet = _wallet()
    request = TransactionRequest(1, "0x" + "4" * 40, "0x", 0, 21_000, 1)
    rpc = _Rpc(["0x1", "0x1dcd6500"])
    signed = []
    monkeypatch.setattr("evm_inventory.route_execution.token_allowance", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution._native_balance", lambda *args, **kwargs: 1_000_000
    )
    monkeypatch.setattr("evm_inventory.route_execution.pending_nonce", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *args, **kwargs: signed.append(args),
    )
    jumper = type("Jumper", (), {"step_transaction": lambda self, step: request})()

    with pytest.raises(EthereumGasDeferred):
        _execute_entry(
            entry=_route_entry(wallet.public_address),
            wallet=wallet,
            rpc=rpc,
            jumper=jumper,
            rpc_urls={1: "https://rpc"},
        )

    assert signed == []
    assert "eth_sendRawTransaction" not in rpc.calls


def test_approval_and_main_transaction_get_fresh_ethereum_gas_probes(monkeypatch):
    wallet = _wallet()
    request = TransactionRequest(1, "0x" + "4" * 40, "0x", 0, 21_000, 1)
    rpc = _Rpc(
        [
            "0x1", "0x1",  # approval guard
            "0x1", "0x1",  # main guard
        ]
    )
    monkeypatch.setattr("evm_inventory.route_execution.token_allowance", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution._native_balance", lambda *args, **kwargs: 1_000_000
    )
    monkeypatch.setattr("evm_inventory.route_execution.pending_nonce", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction", lambda *args, **kwargs: "0x01"
    )
    hashes = iter(["0x" + "a" * 64, "0x" + "b" * 64])
    monkeypatch.setattr(
        "evm_inventory.route_execution.broadcast_signed_transaction",
        lambda *args, **kwargs: next(hashes),
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.wait_for_receipt", lambda *args, **kwargs: {}
    )
    jumper = type("Jumper", (), {"step_transaction": lambda self, step: request})()

    _execute_entry(
        entry=_route_entry(wallet.public_address),
        wallet=wallet,
        rpc=rpc,
        jumper=jumper,
        rpc_urls={1: "https://rpc"},
    )

    assert rpc.calls.count("eth_chainId") == 2
    assert rpc.calls.count("eth_gasPrice") == 2


def test_main_guard_deferral_preserves_confirmed_approval_hash(monkeypatch):
    wallet = _wallet()
    request = TransactionRequest(1, "0x" + "4" * 40, "0x", 0, 21_000, 1)
    rpc = _Rpc(["0x1", "0x1", "0x1", "0x1dcd6500"])
    monkeypatch.setattr("evm_inventory.route_execution.token_allowance", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution._native_balance", lambda *args, **kwargs: 1_000_000
    )
    monkeypatch.setattr("evm_inventory.route_execution.pending_nonce", lambda *args, **kwargs: 0)
    signed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda request, **kwargs: signed.append(request) or "0x01",
    )
    broadcast = []
    monkeypatch.setattr(
        "evm_inventory.route_execution.broadcast_signed_transaction",
        lambda *args, **kwargs: broadcast.append("approval") or "0x" + "a" * 64,
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.wait_for_receipt", lambda *args, **kwargs: {}
    )
    jumper = type("Jumper", (), {"step_transaction": lambda self, step: request})()

    with pytest.raises(EthereumGasDeferred) as exc_info:
        _execute_entry(
            entry=_route_entry(wallet.public_address),
            wallet=wallet,
            rpc=rpc,
            jumper=jumper,
            rpc_urls={1: "https://rpc"},
        )

    assert exc_info.value.approval_tx_hash == "0x" + "a" * 64
    assert len(signed) == 1
    assert broadcast == ["approval"]


def test_expired_quote_after_approval_defers_route_without_route_signature(monkeypatch, tmp_path):
    wallet = _wallet()
    request = TransactionRequest(1, "0x" + "4" * 40, "0x", 0, 21_000, 1)
    clock = [1_000_000]
    signed = []
    broadcast = []
    monkeypatch.setattr("evm_inventory.route_execution.token_allowance", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution._native_balance", lambda *args, **kwargs: 1_000_000
    )
    monkeypatch.setattr("evm_inventory.route_execution.pending_nonce", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "evm_inventory.route_execution.require_ethereum_gas_below_limit",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda transaction, **kwargs: signed.append(transaction) or "0x01",
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.broadcast_signed_transaction",
        lambda *args, **kwargs: broadcast.append("tx") or "0x" + "a" * 64,
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.wait_for_receipt",
        lambda *args, **kwargs: clock.__setitem__(0, clock[0] + 1),
    )
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: _Bitget())
    jumper = type("Jumper", (), {"step_transaction": lambda self, step: request})()
    monkeypatch.setattr("evm_inventory.route_execution.LifiClient", lambda *args, **kwargs: jumper)
    entry = {**_route_entry(wallet.public_address), "quoted_at": 100_000, "settlement": "wallet"}

    summary = execute_entries(
        [entry],
        wallets={wallet.public_address: wallet},
        rpc_urls={1: "https://rpc"},
        journal_path=tmp_path / "journal.sqlite",
        execute=True,
        delay_min_seconds=0,
        delay_max_seconds=0,
        now_ms=lambda: clock[0],
    )

    assert summary == {"submitted": 0, "skipped": 0, "failed": 0, "deferred": 1}
    assert len(signed) == 1
    assert broadcast == ["tx"]
    from evm_inventory.journal import Journal

    with Journal(tmp_path / "journal.sqlite") as journal:
        operation = journal.operation(1)
    assert operation["state"] == "approval_completed_route_deferred"
    assert operation["approval_tx_hash"] == "0x" + "a" * 64
    assert operation["reason"] == "route quote is outside the 15-minute execution window"


def test_batch_records_partial_approval_and_continues_after_deferred_route(monkeypatch, tmp_path):
    wallet = _wallet()
    deferred = EthereumGasDeferred(
        "ethereum_gas_deferred:source=rpc;value_wei=500000000;threshold_wei=500000000"
    )
    deferred.approval_tx_hash = "0x" + "a" * 64
    outcomes = iter([deferred, "0x" + "b" * 64])
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry",
        lambda *args, **kwargs: _raise_or_return(next(outcomes)),
    )
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: _Bitget())

    summary = execute_entries(
        [_direct_entry(wallet.public_address), _direct_entry(wallet.public_address)],
        wallets={wallet.public_address.lower(): wallet},
        rpc_urls={1: "https://rpc"},
        journal_path=tmp_path / "journal.sqlite",
        execute=True,
        delay_min_seconds=0,
        delay_max_seconds=0,
    )

    assert summary == {"submitted": 1, "skipped": 0, "failed": 0, "deferred": 1}
    from evm_inventory.journal import Journal

    with Journal(tmp_path / "journal.sqlite") as journal:
        assert journal.operation(1)["state"] == "approval_completed_route_deferred"
        assert journal.operation(1)["tx_hash"] is None
        assert journal.operation(1)["approval_tx_hash"] == "0x" + "a" * 64
        assert journal.operation(2)["state"] == "completed"


def test_batch_continues_after_real_initial_gas_deferral_without_first_signature(
    monkeypatch, tmp_path
):
    wallet = _wallet()
    rpc = _Rpc(
        [
            "0x1", "0x100000", "0x100000", "0x0", "0x1", "0x1dcd6500",  # deferred first
            "0x1", "0x100000", "0x100000", "0x1", "0x1", "0x1",  # accepted second
            lambda: signed_transaction_hash(raw_transactions[0]), {"status": "0x1"},
        ]
    )
    monkeypatch.setattr("evm_inventory.route_execution.ExecutionRpc", lambda transport: rpc)
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: _Bitget())
    signed = []
    raw_transactions = []
    def signed_transaction(request, **_kwargs):
        signed.append(request)
        raw_transactions.append(sign_transaction(
            request,
            private_key=wallet.private_key,
            expected_sender=wallet.public_address,
            nonce=0,
        ))
        return raw_transactions[-1]

    monkeypatch.setattr("evm_inventory.route_execution.sign_transaction", signed_transaction)

    summary = execute_entries(
        [_direct_entry(wallet.public_address), _direct_entry(wallet.public_address)],
        wallets={wallet.public_address.lower(): wallet},
        rpc_urls={1: "https://rpc"},
        journal_path=tmp_path / "journal.sqlite",
        execute=True,
        delay_min_seconds=0,
        delay_max_seconds=0,
    )

    assert summary == {"submitted": 1, "skipped": 0, "failed": 0, "deferred": 1}
    assert len(signed) == 1
    assert rpc.calls.count("eth_sendRawTransaction") == 1
    from evm_inventory.journal import Journal

    with Journal(tmp_path / "journal.sqlite") as journal:
        assert journal.operation(1)["state"] == "deferred"
        assert journal.operation(1)["tx_hash"] is None
        assert journal.operation(2)["state"] == "completed"


def test_wallet_settled_route_is_staged_without_calling_bitget(monkeypatch, tmp_path):
    wallet = _wallet()
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry", lambda *args, **kwargs: "0x" + "b" * 64
    )

    class Bitget:
        def wait_for_deposit(self, **kwargs):
            raise AssertionError("wallet-settled bridge must not be checked at Bitget")

    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: Bitget())

    summary = execute_entries(
        [{**_route_entry(wallet.public_address), "settlement": "wallet"}],
        wallets={wallet.public_address.lower(): wallet}, rpc_urls={1: "https://rpc"},
        journal_path=tmp_path / "journal.sqlite", execute=True,
        delay_min_seconds=0, delay_max_seconds=0,
    )

    assert summary == {"submitted": 1, "skipped": 0, "failed": 0, "deferred": 0}
    from evm_inventory.journal import Journal
    with Journal(tmp_path / "journal.sqlite") as journal:
        assert journal.operation(1)["deposit_status"] == "staged"


def test_final_deposit_passes_exact_target_identity_to_bitget(monkeypatch, tmp_path):
    wallet = _wallet()
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry", lambda *args, **kwargs: "0x" + "b" * 64
    )
    bitget = _Bitget()
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: bitget)
    entry = {
        **_direct_entry(wallet.public_address), "status": "post_bridge_deposit",
        "target": {"coin": "USDC", "chain": "BASE", "minimum_raw": "9997"},
    }

    execute_entries(
        [entry], wallets={wallet.public_address.lower(): wallet}, rpc_urls={1: "https://rpc"},
        journal_path=tmp_path / "journal.sqlite", execute=True,
        delay_min_seconds=0, delay_max_seconds=0,
    )

    assert bitget.calls[0]["chain"] == "BASE"
    assert bitget.calls[0]["recipient"] == wallet.bitget_deposit_address
    assert bitget.calls[0]["minimum_raw"] == 9997


def test_bitget_api_failure_is_journaled_as_execution_failed(monkeypatch, tmp_path):
    wallet = _wallet()
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry", lambda *args, **kwargs: "0x" + "b" * 64
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution._bitget_client", lambda: _Bitget(RuntimeError("api"))
    )

    summary = execute_entries(
        [_direct_entry(wallet.public_address)], wallets={wallet.public_address.lower(): wallet},
        rpc_urls={1: "https://rpc"}, journal_path=tmp_path / "journal.sqlite", execute=True,
        delay_min_seconds=0, delay_max_seconds=0,
    )

    assert summary["failed"] == 1
    from evm_inventory.journal import Journal
    with Journal(tmp_path / "journal.sqlite") as journal:
        assert journal.operation(1)["deposit_status"] == "execution_failed"


def test_final_deposit_missing_exchange_chain_is_rejected_before_execution(monkeypatch, tmp_path):
    wallet = _wallet()
    executed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry",
        lambda *args, **kwargs: executed.append(True),
    )

    with pytest.raises(ValueError, match="exchange chain"):
        execute_entries(
            [{**_direct_entry(wallet.public_address), "target": {"coin": "ETH", "minimum_raw": 1}}],
            wallets={wallet.public_address.lower(): wallet}, rpc_urls={1: "https://rpc"},
            journal_path=tmp_path / "journal.sqlite", execute=True,
            delay_min_seconds=0, delay_max_seconds=0,
        )

    assert executed == []


@pytest.mark.parametrize(
    "quoted_at",
    [None, True, "1000000", 1_000_001, 99_999],
    ids=["missing", "boolean", "non_integer", "future", "older_than_15_minutes"],
)
def test_route_quote_is_rejected_before_execution_or_private_key_use(
    monkeypatch, tmp_path, quoted_at
):
    wallet = _wallet()
    executed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry",
        lambda *args, **kwargs: executed.append(True),
    )
    entry = _route_entry(wallet.public_address)
    if quoted_at is None:
        entry.pop("quoted_at", None)
    else:
        entry["quoted_at"] = quoted_at

    with pytest.raises(ValueError, match="route quote"):
        execute_entries(
            [entry],
            wallets={wallet.public_address.lower(): wallet},
            rpc_urls={1: "https://rpc"},
            journal_path=tmp_path / "journal.sqlite",
            execute=True,
            delay_min_seconds=0,
            delay_max_seconds=0,
            now_ms=lambda: 1_000_000,
        )

    assert executed == []


def test_route_quote_at_exactly_15_minutes_is_accepted(monkeypatch, tmp_path):
    wallet = _wallet()
    executed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry",
        lambda *args, **kwargs: executed.append(True) or "0x" + "b" * 64,
    )
    entry = {**_route_entry(wallet.public_address), "quoted_at": 100_000}
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: _Bitget())

    summary = execute_entries(
        [entry],
        wallets={wallet.public_address.lower(): wallet},
        rpc_urls={1: "https://rpc"},
        journal_path=tmp_path / "journal.sqlite",
        execute=True,
        delay_min_seconds=0,
        delay_max_seconds=0,
        now_ms=lambda: 1_000_000,
    )

    assert summary["submitted"] == 1
    assert executed == [True]


def test_route_quote_expiring_during_inter_wallet_delay_is_not_executed(monkeypatch, tmp_path):
    first = _wallet()
    second = WalletWorkbookRow(
        3,
        2,
        Account.from_key("0x" + "2" * 64).address.lower(),
        "0x" + "2" * 64,
        "0x" + "3" * 40,
    )
    executed = []
    clock = [1_000_000]
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry",
        lambda *, entry, **kwargs: executed.append(entry["wallet"]) or "0x" + "b" * 64,
    )
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: _Bitget())
    entries = [
        {**_route_entry(first.public_address), "quoted_at": 100_000, "settlement": "wallet"},
        {**_route_entry(second.public_address), "quoted_at": 100_000, "settlement": "wallet"},
    ]

    with pytest.raises(ValueError, match="route quote"):
        execute_entries(
            entries,
            wallets={first.public_address: first, second.public_address: second},
            rpc_urls={1: "https://rpc"},
            journal_path=tmp_path / "journal.sqlite",
            execute=True,
            delay_min_seconds=1,
            delay_max_seconds=1,
            sleep=lambda _: clock.__setitem__(0, clock[0] + 1),
            wallet_batches=((first.public_address,), (second.public_address,)),
            now_ms=lambda: clock[0],
        )

    assert executed == [first.public_address]


def test_direct_deposit_does_not_require_a_route_quote(monkeypatch, tmp_path):
    wallet = _wallet()
    executed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry",
        lambda *args, **kwargs: executed.append(True) or "0x" + "b" * 64,
    )
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: _Bitget())

    execute_entries(
        [_direct_entry(wallet.public_address)],
        wallets={wallet.public_address.lower(): wallet},
        rpc_urls={1: "https://rpc"},
        journal_path=tmp_path / "journal.sqlite",
        execute=True,
        delay_min_seconds=0,
        delay_max_seconds=0,
        now_ms=lambda: 1_000_000,
    )

    assert executed == [True]


def test_post_bridge_deposit_does_not_require_a_route_quote(monkeypatch, tmp_path):
    wallet = _wallet()
    executed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution._execute_entry",
        lambda *args, **kwargs: executed.append(True) or "0x" + "b" * 64,
    )
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: _Bitget())

    execute_entries(
        [{**_direct_entry(wallet.public_address), "status": "post_bridge_deposit"}],
        wallets={wallet.public_address.lower(): wallet},
        rpc_urls={1: "https://rpc"},
        journal_path=tmp_path / "journal.sqlite",
        execute=True,
        delay_min_seconds=0,
        delay_max_seconds=0,
        now_ms=lambda: 1_000_000,
    )

    assert executed == [True]


def _wallet():
    key = "0x" + "1" * 64
    return WalletWorkbookRow(2, 1, Account.from_key(key).address.lower(), key, "0x" + "2" * 40)


def _direct_entry(wallet):
    return {
        "status": "direct_deposit",
        "wallet": wallet,
        "chain_id": 1,
        "asset_id": "native",
        "target": {"minimum_raw": 1, "coin": "ETH", "chain": "ETH"},
    }


def _route_entry(wallet):
    return {
        "status": "route_ready",
        "wallet": wallet,
        "chain_id": 1,
        "asset_id": "0x" + "3" * 40,
        "raw_balance": 1,
        "quoted_at": time.time_ns() // 1_000_000,
        "target": {"coin": "ETH"},
        "route": {"step": {"estimate": {"approvalAddress": "0x" + "5" * 40}}},
    }


def _raise_or_return(value):
    if isinstance(value, Exception):
        raise value
    return value


class _Bitget:
    def __init__(self, outcome="success"):
        self.outcome = outcome
        self.calls = []

    def wait_for_deposit(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome
