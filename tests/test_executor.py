import pytest
from eth_account import Account

from evm_inventory.executor import (
    EthereumGasDeferred,
    require_ethereum_gas_below_limit,
    require_native_reserve,
    sign_transaction,
)
from evm_inventory.lifi import TransactionRequest
from evm_inventory.route_execution import _execute_entry, execute_entries
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


def test_sign_transaction_rejects_wrong_sender():
    request = TransactionRequest(10, "0x" + "2" * 40, "0x", 0, 21_000, 1)

    with pytest.raises(ValueError, match="private key"):
        sign_transaction(
            request,
            private_key="0x" + "1" * 64,
            expected_sender="0x" + "3" * 40,
            nonce=0,
        )


def test_native_reserve_requires_five_times_estimated_gas():
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


class _Rpc:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, url, method, params):
        self.calls.append(method)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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
            "0x" + "b" * 64, {"status": "0x1"},
        ]
    )
    monkeypatch.setattr("evm_inventory.route_execution.ExecutionRpc", lambda transport: rpc)
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: _Bitget())
    signed = []
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda request, **kwargs: signed.append(request)
        or sign_transaction(
            request,
            private_key=wallet.private_key,
            expected_sender=wallet.public_address,
            nonce=0,
        ),
    )

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
