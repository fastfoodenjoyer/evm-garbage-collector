from types import SimpleNamespace

import pytest
from eth_utils import keccak

from evm_inventory.journal import Journal
from evm_inventory.relay_execution import _destination_transfer, _observe
from evm_inventory.rpc import RpcError

SOURCE_HASH = "0x" + "1" * 64
DESTINATION_HASH = "0x" + "2" * 64
WALLET = "0x" + "3" * 40
RECIPIENT = "0x" + "4" * 40
TOKEN = "0x" + "5" * 40
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


def _checkpoint():
    return {"source_chain_id": 1868, "recipient": RECIPIENT,
        "target": {"chain_id": 8453, "asset_id": TOKEN, "coin": "USDC", "chain": "BASE"},
        "quote": {"to_amount_min": "100"}, "request_id": "0x" + "a" * 64,
        "started_ms": 100000}


def _receipt(data=None):
    return {"transactionHash": DESTINATION_HASH, "status": "0x1", "blockNumber": "0x10",
        "logs": [{"address": TOKEN, "topics": [TRANSFER,
            "0x" + WALLET[2:].rjust(64, "0"), "0x" + RECIPIENT[2:].rjust(64, "0")],
            "data": data if data is not None else "0x" + f"{150:064x}"}]}


def _rpc(receipt):
    def call(url, method, params):
        if method == "eth_chainId":
            return hex(1868 if url == "source" else 8453)
        if method == "eth_getBlockByNumber":
            return {"number": "0x12"}
        if method == "eth_getTransactionReceipt":
            if params == [SOURCE_HASH]:
                return {"transactionHash": SOURCE_HASH, "status": "0x1", "blockNumber": "0x10"}
            assert params == [DESTINATION_HASH]
            return receipt
        pytest.fail("Observer must only read source/destination receipts: " + method)
    return SimpleNamespace(call=call)


def test_relay_destination_decodes_abi_transfer_word_with_leading_zeroes():
    assert _destination_transfer(_rpc(_receipt()), {8453: "destination"}, _checkpoint(),
                                 [DESTINATION_HASH]) == DESTINATION_HASH


@pytest.mark.parametrize("data", ["0x1", "0x", "0x" + "00" * 31, "0x" + "00" * 33])
def test_relay_destination_rejects_transfer_data_that_is_not_one_uint256(data):
    with pytest.raises(RpcError, match="invalid_abi_uint256"):
        _destination_transfer(_rpc(_receipt(data)), {8453: "destination"}, _checkpoint(),
                              [DESTINATION_HASH])


def test_relay_observer_pending_to_credit_uses_saved_hashes_without_send(tmp_path):
    statuses = iter([None, "pending", "success"])
    polls = []
    def deposit_status(**kwargs):
        polls.append(kwargs)
        return next(statuses)
    client = SimpleNamespace(status=lambda _: {"status": "success", "originChainId": 1868,
        "destinationChainId": 8453, "inTxHashes": [SOURCE_HASH], "txHashes": [DESTINATION_HASH]})
    with Journal(tmp_path / "inventory.sqlite") as journal:
        position = journal.get_or_create_position(position_key="relay", wallet=WALLET)
        journal.record_step_intent(position_id=position["id"], step_key="relay_deposit", nonce=1,
            calldata_digest="b" * 64, signed_payload_digest="c" * 64, tx_hash=SOURCE_HASH)
        journal.record_position_state(position["id"], "bridge_submitted")
        checkpoint = _checkpoint()
        journal.save_checkpoint(position["id"], "relay_execution", checkpoint)
        kw = dict(client=client, rpc=_rpc(_receipt()),
                  rpc_urls={1868: "source", 8453: "destination"},
                  bitget=SimpleNamespace(deposit_status=deposit_status))
        assert _observe(journal, position, checkpoint, **kw) == "pending"
        assert _observe(journal, position, checkpoint, **kw) == "pending"
        assert _observe(journal, position, checkpoint, **kw) == "completed"
        assert journal.position(position["id"])["state"] == "completed"
        credit = journal.checkpoint(position["id"], "bitget_credit")
        assert credit["status"] == "success" and credit["tx_hash"] == DESTINATION_HASH
    assert len(polls) == 3
    assert all(p["tx_hash"] == DESTINATION_HASH and p["recipient"] == RECIPIENT for p in polls)
