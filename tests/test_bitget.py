import httpx
import pytest

from evm_inventory.bitget import BitgetClient


def test_deposit_status_matches_transaction_hash_fields():
    class Client(BitgetClient):
        def deposit_records(self, **_):
            return ({
                "txId": "0xabc", "coin": "USDC", "chain": "BASE",
                "address": "0x" + "1" * 40, "size": "0.01", "status": "success",
            },)

    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    assert client.deposit_status(
        tx_hash="0xAbC", start_ms=1, end_ms=2, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=10_000,
    ) == "success"


def test_deposit_status_matches_real_bitget_recipient_field_and_chain_case():
    class Client(BitgetClient):
        def deposit_records(self, **_):
            return ({
                "tradeId": "0xabc", "coin": "ETH", "chain": "Optimism",
                "toAddress": "0x" + "1" * 40, "size": "0.01", "status": "success",
            },)

    client = Client(api_key="key", secret_key="secret", passphrase="pass")
    assert client.deposit_status(
        tx_hash="0xabc", start_ms=1, end_ms=2, coin="ETH", chain="OPTIMISM",
        recipient="0x" + "1" * 40, minimum_raw=10**16,
    ) == "success"


@pytest.mark.parametrize(
    "field,value",
    [
        ("chain", "ETH"),
        ("address", "0x" + "2" * 40),
        ("size", "0.009999"),
    ],
)
def test_deposit_status_rejects_matching_hash_with_wrong_identity_field(field, value):
    record = {
        "txHash": "0xabc", "coin": "USDC", "chain": "BASE",
        "address": "0x" + "1" * 40, "size": "0.01", "status": "success",
    }
    record[field] = value

    class Client(BitgetClient):
        def deposit_records(self, **_):
            return (record,)

    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    assert client.deposit_status(
        tx_hash="0xabc", start_ms=1, end_ms=2, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=10_000,
    ) is None


@pytest.mark.parametrize("size", ("NaN", "Infinity", "-Infinity"))
def test_deposit_status_rejects_non_finite_record_amounts(size):
    class Client(BitgetClient):
        def deposit_records(self, **_):
            return ({
                "txHash": "0xabc", "coin": "USDC", "chain": "BASE",
                "address": "0x" + "1" * 40, "size": size, "status": "success",
            },)

    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    assert client.deposit_status(
        tx_hash="0xabc", start_ms=1, end_ms=2, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=1,
    ) is None


def test_wait_for_deposit_accepts_success_case_insensitively(monkeypatch):
    class Client(BitgetClient):
        def deposit_status(self, **_):
            return "SUCCESS"

    monotonic = iter((0, 0)).__next__
    monkeypatch.setattr("evm_inventory.bitget.time.monotonic", monotonic)
    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    assert client.wait_for_deposit(
        tx_hash="0xabc", started_ms=1, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=1, timeout_seconds=1, poll_seconds=0
    ) == "SUCCESS"


def test_wait_for_deposit_seen_returns_pending_without_waiting_for_success(monkeypatch):
    class Client(BitgetClient):
        statuses = iter((None, "pending"))

        def deposit_status(self, **_):
            return next(self.statuses)

    monkeypatch.setattr("evm_inventory.bitget.time.monotonic", lambda: 0)
    monkeypatch.setattr("evm_inventory.bitget.time.sleep", lambda _: None)
    client = Client(api_key="key", secret_key="secret", passphrase="pass")
    assert client.wait_for_deposit_seen(
        tx_hash="0xabc", started_ms=1, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=1,
        timeout_seconds=1, poll_seconds=0,
    ) == "pending"


def test_wait_for_deposit_uses_valid_range_in_same_millisecond(monkeypatch):
    class Client(BitgetClient):
        def deposit_status(self, **kwargs):
            assert kwargs["end_ms"] > kwargs["start_ms"] == 1000
            return "success"

    monkeypatch.setattr("evm_inventory.bitget.time.monotonic", lambda: 0)
    monkeypatch.setattr("evm_inventory.bitget.time.time", lambda: 1.0)
    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    assert client.wait_for_deposit(
        tx_hash="0xabc", started_ms=1000, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=1, timeout_seconds=1, poll_seconds=0
    ) == "success"


def test_wait_for_deposit_retries_temporary_api_error(monkeypatch):
    request = httpx.Request("GET", "https://api.bitget.com/deposits")
    response = httpx.Response(429, request=request)
    attempts = []

    class Client(BitgetClient):
        def deposit_status(self, **_kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return "success"

    monkeypatch.setattr("evm_inventory.bitget.time.monotonic", lambda: 0)
    monkeypatch.setattr("evm_inventory.bitget.time.sleep", lambda _: None)
    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    assert client.wait_for_deposit(
        tx_hash="0xabc", started_ms=1000, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=1, timeout_seconds=1, poll_seconds=0
    ) == "success"
    assert len(attempts) == 2


def test_wait_for_deposit_polls_pending_status_until_success(monkeypatch):
    class Client(BitgetClient):
        statuses = iter(("pending", "success"))

        def deposit_status(self, **_):
            return next(self.statuses)

    monotonic = iter((0, 0, 0)).__next__
    monkeypatch.setattr("evm_inventory.bitget.time.monotonic", monotonic)
    monkeypatch.setattr("evm_inventory.bitget.time.sleep", lambda _: None)
    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    assert client.wait_for_deposit(
        tx_hash="0xabc", started_ms=1, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=1, timeout_seconds=1, poll_seconds=0
    ) == "success"


@pytest.mark.parametrize("status", (None, "", "unexpected"))
def test_wait_for_deposit_does_not_credit_empty_failed_or_unknown_status(monkeypatch, status):
    class Client(BitgetClient):
        def deposit_status(self, **_):
            return status

    monotonic = iter((0, 0, 1)).__next__
    monkeypatch.setattr("evm_inventory.bitget.time.monotonic", monotonic)
    monkeypatch.setattr("evm_inventory.bitget.time.sleep", lambda _: None)
    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    assert client.wait_for_deposit(
        tx_hash="0xabc", started_ms=1, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=1, timeout_seconds=1, poll_seconds=0
    ) is None


def test_wait_for_deposit_stops_on_failed_record(monkeypatch):
    class Client(BitgetClient):
        def deposit_status(self, **_):
            return "fail"

    monkeypatch.setattr("evm_inventory.bitget.time.monotonic", lambda: 0)
    client = Client(api_key="key", secret_key="secret", passphrase="pass")
    assert client.wait_for_deposit(
        tx_hash="0xabc", started_ms=1, coin="USDC", chain="BASE",
        recipient="0x" + "1" * 40, minimum_raw=1,
        timeout_seconds=1, poll_seconds=0,
    ) == "fail"


def test_wait_for_deposit_propagates_deposit_api_errors(monkeypatch):
    class Client(BitgetClient):
        def deposit_status(self, **_):
            raise RuntimeError("Bitget API unavailable")

    monotonic = iter((0, 0)).__next__
    monkeypatch.setattr("evm_inventory.bitget.time.monotonic", monotonic)
    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    with pytest.raises(RuntimeError, match="Bitget API unavailable"):
        client.wait_for_deposit(
            tx_hash="0xabc", started_ms=1, coin="USDC", chain="BASE",
            recipient="0x" + "1" * 40, minimum_raw=1, timeout_seconds=1, poll_seconds=0
        )
