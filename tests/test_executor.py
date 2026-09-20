import pytest
from eth_account import Account

from evm_inventory.executor import require_native_reserve, sign_transaction
from evm_inventory.lifi import TransactionRequest
from evm_inventory.route_execution import execute_entries


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
