"""Validate native EVM Relay orders before allowing a signature.

Schema: @relay-protocol/settlement-sdk 0.0.146 ORDER_EIP712_TYPES.
https://docs.relay.link/references/api/api_core_concepts/input-validation
"""

from __future__ import annotations

import copy
import re
import time

from eth_abi import encode
from eth_account._utils.encode_typed_data.encoding_and_hashing import hash_struct
from eth_utils import keccak

from .lifi import TransactionRequest

ORDER_TYPES = {
    "Order": [
        {"name": "version", "type": "string"},
        {"name": "solverChainId", "type": "string"},
        {"name": "solver", "type": "address"},
        {"name": "salt", "type": "uint256"},
        {"name": "inputs", "type": "Input[]"},
        {"name": "output", "type": "Output"},
        {"name": "fees", "type": "Fee[]"},
    ],
    "Input": [
        {"name": "payment", "type": "InputPayment"},
        {"name": "refunds", "type": "InputRefund[]"},
    ],
    "InputPayment": [
        {"name": "chainId", "type": "string"},
        {"name": "currency", "type": "bytes"},
        {"name": "amount", "type": "uint256"},
        {"name": "weight", "type": "uint256"},
    ],
    "InputRefund": [
        {"name": "chainId", "type": "string"},
        {"name": "recipient", "type": "bytes"},
        {"name": "currency", "type": "bytes"},
        {"name": "minimumAmount", "type": "uint256"},
        {"name": "deadline", "type": "uint32"},
        {"name": "extraData", "type": "bytes"},
    ],
    "Output": [
        {"name": "chainId", "type": "string"},
        {"name": "payments", "type": "OutputPayment[]"},
        {"name": "deadline", "type": "uint32"},
        {"name": "calls", "type": "bytes[]"},
        {"name": "extraData", "type": "bytes"},
    ],
    "OutputPayment": [
        {"name": "recipient", "type": "bytes"},
        {"name": "currency", "type": "bytes"},
        {"name": "minimumAmount", "type": "uint256"},
        {"name": "expectedAmount", "type": "uint256"},
    ],
    "Fee": [
        {"name": "recipientChainId", "type": "string"},
        {"name": "recipient", "type": "bytes"},
        {"name": "currencyChainId", "type": "string"},
        {"name": "currency", "type": "bytes"},
        {"name": "amount", "type": "uint256"},
    ],
}


def verified_transaction(quote, chains):
    request, body = quote["request"], quote["response"]
    protocol = body["protocol"]["v2"]
    order = copy.deepcopy(protocol["orderData"])
    catalog = {c["id"]: c for c in chains}
    origin = catalog[request["originChainId"]]
    destination = catalog[request["destinationChainId"]]
    for chain in (origin, destination):
        if chain.get("vmType") != "evm" or chain.get("disabled"):
            raise ValueError("Relay execution supports enabled EVM chains only")
    source_id = origin["protocol"]["v2"]["chainId"]
    destination_id = destination["protocol"]["v2"]["chainId"]
    depository = origin["protocol"]["v2"]["depository"].lower()
    if not origin.get("depositEnabled") or protocol.get("hubType") != "onchain":
        raise ValueError("Relay native deposits unavailable")
    if order["version"] != "v1" or len(order["inputs"]) != 1 or order["fees"]:
        raise ValueError("Unsupported Relay order version, inputs or explicit fees")
    payment = order["inputs"][0]["payment"]
    output = order["output"]
    zero = "0x" + "0" * 40
    if (
        request["originCurrency"] != zero
        or payment["currency"].lower() != zero
        or payment["chainId"] != source_id
        or int(payment["amount"]) != int(request["amount"])
        or int(payment["weight"]) != 1
    ):
        raise ValueError("Relay native input does not match the approved request")
    if output["chainId"] != destination_id or output["calls"] or len(output["payments"]) != 1:
        raise ValueError("Unsupported Relay output chain, calls or multiple recipients")
    outgoing = output["payments"][0]
    if (
        outgoing["recipient"].lower() != request["recipient"].lower()
        or outgoing["currency"].lower() != request["destinationCurrency"]
        or int(outgoing["minimumAmount"]) != int(quote["to_amount_min"])
        or int(outgoing["expectedAmount"]) != int(quote["to_amount"])
    ):
        raise ValueError("Relay protocol output differs from the approved quote")
    if int(output["deadline"]) <= time.time() + 60:
        raise ValueError("Relay order is expired or expires before safe submission")
    evm_ids = {
        c.get("protocol", {}).get("v2", {}).get("chainId")
        for c in chains
        if c.get("vmType") == "evm"
    }
    if order["solverChainId"] not in evm_ids:
        raise ValueError("Relay solver VM is unsupported")
    allowed_refunds = {
        (source_id, request["user"].lower(), request["originCurrency"]),
        (destination_id, request["recipient"].lower(), request["destinationCurrency"]),
    }
    for refund in order["inputs"][0]["refunds"]:
        # Relay orders may refund on either leg. Only the exact source wallet
        # and approved destination payment identities are authorized here.
        identity = (refund["chainId"], refund["recipient"].lower(), refund["currency"].lower())
        if identity not in allowed_refunds:
            raise ValueError("Relay refund differs from the authorized source or destination")
    # EVM addresses encode to their original 20 bytes in the settlement SDK.
    order["salt"] = (
        int(str(order["salt"]), 0) if str(order["salt"]).startswith("0x") else int(order["salt"])
    )
    order_id = "0x" + hash_struct("Order", ORDER_TYPES, order).hex()
    if protocol["orderId"].lower() != order_id:
        raise ValueError("Relay order hash does not match its terms")
    details = protocol["paymentDetails"]
    if (
        details["depository"].lower() != depository
        or details["chainId"] != source_id
        or details["currency"].lower() != zero
        or int(details["amount"]) != int(payment["amount"])
    ):
        raise ValueError("Relay payment details differ from the canonical order")
    if len(body["steps"]) != 1:
        raise ValueError("Relay multi-step execution is unsupported")
    step = body["steps"][0]
    if (
        step["id"] != "deposit"
        or step["kind"] != "transaction"
        or step.get("depositAddress")
        or len(step["items"]) != 1
        or not re.fullmatch(r"0x[0-9a-fA-F]{64}", step["requestId"])
    ):
        raise ValueError("Unsupported Relay deposit step")
    tx = step["items"][0]["data"]
    calldata = (
        "0x"
        + (
            keccak(text="depositNative(address,bytes32)")[:4]
            + encode(["address", "bytes32"], [request["user"], bytes.fromhex(order_id[2:])])
        ).hex()
    )
    if (
        tx["to"].lower() != depository
        or tx["from"].lower() != request["user"].lower()
        or int(tx["chainId"]) != request["originChainId"]
        or int(tx["value"]) != int(request["amount"])
        or tx["data"].lower() != calldata
    ):
        raise ValueError("Relay transaction does not commit to the verified native order")
    return TransactionRequest(
        chain_id=request["originChainId"],
        to=depository,
        data=calldata,
        value=int(request["amount"]),
        gas_limit=0,
        gas_price_wei=0,
    )
