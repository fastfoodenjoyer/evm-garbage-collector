"""Exact canonical WETH withdrawals, with no router or token approval."""

from hashlib import sha256

WETH_CONTRACTS = {
    1: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    10: "0x4200000000000000000000000000000000000006",
    8453: "0x4200000000000000000000000000000000000006",
}
WETH_WITHDRAW_TOOL = "canonical-weth-withdraw"
NATIVE = "0x0000000000000000000000000000000000000000"


def is_unwrap_request(request) -> bool:
    return (
        request.from_chain_id == request.to_chain_id
        and request.from_token_address.lower() == WETH_CONTRACTS.get(request.from_chain_id)
        and request.to_token_address.lower() == NATIVE
        and request.from_address.lower() == request.to_address.lower()
    )


def unwrap_route_payload(request, source_price: str, native_price: str) -> dict:
    amount = int(request.from_amount)
    if not is_unwrap_request(request) or not 0 < amount < 2**256:
        raise ValueError("invalid canonical WETH withdrawal request")
    chain = request.from_chain_id
    source = {"chainId": chain, "address": WETH_CONTRACTS[chain], "decimals": 18,
              "priceUSD": source_price}
    destination = {"chainId": chain, "address": NATIVE, "decimals": 18,
                   "priceUSD": native_price}
    action = {"fromChainId": chain, "toChainId": chain, "fromToken": source,
              "toToken": destination, "fromAmount": str(amount),
              "fromAddress": request.from_address, "toAddress": request.to_address}
    identifier = sha256(str((chain, amount, request.from_address.lower())).encode()).hexdigest()
    step = {"id": identifier, "type": "protocol", "tool": WETH_WITHDRAW_TOOL,
            "action": action, "estimate": {"fromAmount": str(amount), "toAmount": str(amount),
            "toAmountMin": str(amount), "gasCosts": [], "feeCosts": []}}
    return {"id": identifier, "fromAmount": str(amount), "toAmount": str(amount),
            "toAmountMin": str(amount), "steps": [step]}


def withdraw_transaction_fields(step: dict) -> tuple[int, str, str]:
    """Validate all fields before generating fixed withdraw(uint256) calldata."""
    action = step.get("action", {})
    chain = action.get("fromChainId")
    source = action.get("fromToken", {})
    destination = action.get("toToken", {})
    amount = int(action.get("fromAmount", "0"))
    estimate = step.get("estimate", {})
    if (
        step.get("tool") != WETH_WITHDRAW_TOOL or step.get("type") != "protocol"
        or chain not in WETH_CONTRACTS or action.get("toChainId") != chain
        or source.get("chainId") != chain or destination.get("chainId") != chain
        or source.get("address", "").lower() != WETH_CONTRACTS[chain]
        or source.get("decimals") != 18 or destination.get("decimals") != 18
        or destination.get("address", "").lower() != NATIVE
        or not isinstance(action.get("fromAddress"), str)
        or action["fromAddress"].lower() != str(action.get("toAddress", "")).lower()
        or not 0 < amount < 2**256
        or int(estimate.get("toAmount", "0")) != amount
        or int(estimate.get("toAmountMin", "0")) != amount
        or estimate.get("approvalAddress")
        or estimate.get("feeCosts")
        or step.get("includedSteps") or step.get("steps") or step.get("substeps")
    ):
        raise ValueError("invalid canonical WETH withdrawal step")
    return chain, WETH_CONTRACTS[chain], "0x2e1a7d4d" + f"{amount:064x}"
