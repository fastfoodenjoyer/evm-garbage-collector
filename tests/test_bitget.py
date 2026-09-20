from evm_inventory.bitget import BitgetClient


def test_deposit_status_matches_transaction_hash_fields():
    class Client(BitgetClient):
        def deposit_records(self, **_):
            return ({"txId": "0xabc", "status": "success"},)

    client = Client(api_key="key", secret_key="secret", passphrase="pass")

    assert client.deposit_status(tx_hash="0xAbC", start_ms=1, end_ms=2, coin="USDC") == "success"
