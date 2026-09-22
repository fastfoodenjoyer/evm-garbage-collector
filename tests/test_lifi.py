import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from evm_inventory.consolidation import candidate_from_route
from evm_inventory.lifi import (
    LifiClient,
    LifiError,
    LifiRouteRequest,
    _route_from_dict,
    validate_bridge_route,
)
from evm_inventory.models import AssetIdentity

SOURCE_TOKEN = "0x" + "a" * 40
DESTINATION_TOKEN = "0x" + "b" * 40
WALLET = "0x" + "1" * 40
QUOTE_NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)


class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "routes": [
                {
                    "id": "route-1",
                    "fromAmount": "1000000",
                    "toAmount": "998000",
                    "toAmountMin": "990000",
                    "gasCosts": [
                        {
                            "amount": "21000",
                            "amountUSD": "0.03",
                            "token": {
                                "chainId": 10,
                                "address": "0x0000000000000000000000000000000000000000",
                                "decimals": 18,
                            },
                            "priceUSD": "2500",
                        }
                    ],
                    "steps": [_bridge_step()],
                }
            ],
            "unavailableRoutes": [],
        }


class HttpClient:
    def __init__(self):
        self.calls = []

    def post(self, url, *, json, headers, timeout):
        self.calls.append((url, json, headers, timeout))
        return Response()


def test_lifi_routes_use_read_only_quote_endpoint_and_parse_costs():
    http = HttpClient()
    client = LifiClient(http)
    request = LifiRouteRequest(
        from_chain_id=10,
        to_chain_id=8453,
        from_token_address=SOURCE_TOKEN,
        to_token_address=DESTINATION_TOKEN,
        from_amount="1000000",
        from_address=WALLET,
        to_address=WALLET,
    )

    routes = client.routes(request)

    assert http.calls[0][0] == "https://api.jumper.xyz/pipeline/v1/advanced/routes"
    assert http.calls[0][1]["fromAddress"] == request.from_address
    assert http.calls[0][1]["options"]["order"] == "CHEAPEST"
    assert routes[0].to_amount_min == 990000
    assert routes[0].gas_costs[0].amount == 21000
    assert routes[0].tools == ("across",)
    assert routes[0].action.source == AssetIdentity(10, SOURCE_TOKEN, 6)
    assert routes[0].action.recipient == WALLET


def test_validated_bridge_quote_requires_exact_identities_and_recipient():
    route = LifiClient(HttpClient()).routes(
        LifiRouteRequest(10, 8453, SOURCE_TOKEN, DESTINATION_TOKEN, "1000000", WALLET, WALLET)
    )[0]

    assert validate_bridge_route(
        route,
        expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
        expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
        recipient=WALLET,
        input_amount=1_000_000,
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda step: step.update({"includedSteps": [{"type": "swap"}]}), "substeps"),
        (lambda step: step["action"].update({"toAddress": "0x" + "2" * 40}), "recipient"),
        (lambda step: step["action"].update({"fromAmount": "9"}), "amount"),
        (lambda step: step["action"].update({"fromChainId": 1}), "source identity"),
        (lambda step: step["action"].update({"toChainId": 1}), "destination identity"),
    ],
)
def test_lifi_rejects_hidden_or_mismatched_bridge_action(mutate, message):
    step = _bridge_step()
    mutate(step)

    class InvalidResponse(Response):
        def json(self):
            return {
                "routes": [
                    {
                        "id": "route-1",
                        "fromAmount": "1000000",
                        "toAmount": "998000",
                        "toAmountMin": "990000",
                        "steps": [step],
                    }
                ]
            }

    class InvalidHttp(HttpClient):
        def post(self, url, *, json, headers, timeout):
            return InvalidResponse()

    route = LifiClient(InvalidHttp()).routes(
        LifiRouteRequest(10, 8453, SOURCE_TOKEN, DESTINATION_TOKEN, "1000000", WALLET, WALLET)
    )[0]
    with pytest.raises(ValueError, match=message):
        validate_bridge_route(
            route,
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )


def test_validated_bridge_quote_does_not_use_a_provider_allowlist():
    step = _bridge_step()
    step["tool"] = "new-live-provider"
    route = _route_from_dict(
        {
            "id": "route-1",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "steps": [step],
        }
    )

    assert validate_bridge_route(
        route,
        expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
        expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
        recipient=WALLET,
        input_amount=1_000_000,
    ) is route


def test_lifi_rejects_routes_without_one_executable_action_or_route_id():
    with pytest.raises(ValueError, match="route"):
        _route_from_dict({})


def _bridge_step():
    return {
        "id": "step-1",
        "type": "cross",
        "tool": "across",
        "action": {
            "fromChainId": 10,
            "toChainId": 8453,
            "fromToken": {"address": SOURCE_TOKEN, "decimals": 6},
            "toToken": {"address": DESTINATION_TOKEN, "decimals": 6},
            "fromAmount": "1000000",
            "toAddress": WALLET,
        },
        "estimate": {
            "feeCosts": [
                {
                    "amount": "100",
                    "amountUSD": "0.01",
                    "token": {"chainId": 10, "address": SOURCE_TOKEN, "decimals": 6},
                }
            ]
        },
    }


def _two_step_route(*, second_token=None, second_amount="800000000000000000"):
    intermediate = "0x" + "c" * 40
    first = {
        "id": "first",
        "type": "swap",
        "tool": "provider-a",
        "action": {
            "fromChainId": 10,
            "toChainId": 10,
            "fromToken": {"address": SOURCE_TOKEN, "decimals": 6},
            "toToken": {"address": intermediate, "decimals": 18},
            "fromAmount": "1000000",
            "toAddress": WALLET,
        },
        "estimate": {"toAmount": "800000000000000000"},
    }
    second = {
        "id": "second",
        "type": "cross",
        "tool": "provider-b",
        "action": {
            "fromChainId": 10,
            "toChainId": 8453,
            "fromToken": {
                "address": second_token or intermediate,
                "decimals": 18,
            },
            "toToken": {"address": DESTINATION_TOKEN, "decimals": 6},
            "fromAmount": second_amount,
            "toAddress": WALLET,
        },
        "estimate": {"toAmount": "700000"},
    }
    return _route_from_dict(
        {
            "id": "two-steps",
            "fromAmount": "1000000",
            "toAmount": "700000",
            "toAmountMin": "690000",
            "steps": [first, second],
        }
    )


def test_lifi_step_transaction_parses_unsigned_transaction():
    class StepResponse(Response):
        def json(self):
            return {
                "transactionRequest": {
                    "chainId": 10,
                    "to": "0x" + "2" * 40,
                    "data": "0x1234",
                    "value": "0x0",
                    "gasLimit": "0x5208",
                    "gasPrice": "0x3b9aca00",
                }
            }

    class StepHttp(HttpClient):
        def post(self, url, *, json, headers, timeout):
            self.calls.append((url, json, headers, timeout))
            return StepResponse()

    transaction = LifiClient(StepHttp()).step_transaction({"tool": "across"})

    assert transaction.chain_id == 10
    assert transaction.gas_limit == 21000
    assert transaction.to == "0x" + "2" * 40


def test_route_evidence_normalizes_identities_amounts_recipient_prices_and_payload_hash():
    payload = {
        "id": "route-evidence",
        "fromAmount": "1000000",
        "toAmount": "990000",
        "toAmountMin": "980000",
        "fromTokenPriceUSD": "1.25",
        "toTokenPriceUSD": "1.20",
        "steps": [_bridge_step()],
    }

    route = _route_from_dict(payload)
    evidence = route.evidence

    assert evidence.route_id == "route-evidence"
    assert evidence.source == AssetIdentity(10, SOURCE_TOKEN, 6)
    assert evidence.destination == AssetIdentity(8453, DESTINATION_TOKEN, 6)
    assert evidence.input_amount == 1_000_000
    assert evidence.output_amount == 990_000
    assert evidence.min_output_amount == 980_000
    assert evidence.final_recipient == WALLET.lower()
    assert len(evidence.steps) == 1
    assert evidence.quote_timestamp.endswith("Z")
    assert {price.asset for price in evidence.prices} == {
        evidence.source,
        evidence.destination,
    }
    assert evidence.payload_sha256 == hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_route_evidence_normalizes_every_executable_step_and_accepts_multi_step_route():
    intermediate = "0x" + "c" * 40
    first = {
        "id": "swap-step",
        "type": "swap",
        "tool": "arbitrary-swap-provider",
        "action": {
            "fromChainId": 10,
            "toChainId": 10,
            "fromToken": {"address": SOURCE_TOKEN, "decimals": 6, "priceUSD": "1.5"},
            "toToken": {"address": intermediate, "decimals": 18, "priceUSD": "2.5"},
            "fromAmount": "1000000",
            "toAddress": WALLET,
        },
        "estimate": {
            "toAmount": "800000000000000000",
            "feeCosts": [
                {
                    "amount": "5",
                    "amountUSD": "0.01",
                    "priceUSD": "0.4",
                    "token": {
                        "chainId": 10,
                        "address": SOURCE_TOKEN,
                        "decimals": 6,
                    },
                }
            ],
            "gasCosts": [
                {
                    "amount": "7",
                    "amountUSD": "0.02",
                    "priceUSD": "1.7",
                    "token": {
                        "chainId": 10,
                        "address": "0x" + "e" * 40,
                        "decimals": 18,
                    },
                }
            ],
        },
    }
    final = _bridge_step()
    final["action"].update(
        {
                "fromToken": {
                    "address": intermediate,
                    "decimals": 18,
                    "priceUSD": "2.75",
                },
            "toToken": {"address": DESTINATION_TOKEN, "decimals": 6, "priceUSD": "3.5"},
            "fromAmount": "800000000000000000",
            "toAddress": WALLET.upper().replace("0X", "0x"),
        }
    )
    final["estimate"]["feeCosts"] = [
        {
            "amount": "9",
            "amountUSD": "0.03",
            "priceUSD": "0.6",
            "token": {
                "chainId": 10,
                "address": "0x" + "d" * 40,
                "decimals": 8,
            },
        }
    ]
    final["estimate"]["gasCosts"] = [
        {
            "amount": "11",
            "amountUSD": "0.04",
            "priceUSD": "2.1",
            "token": {
                "chainId": 8453,
                "address": "0x" + "e" * 40,
                "decimals": 18,
            },
        }
    ]
    final["estimate"]["toAmount"] = "970000"
    route = _route_from_dict(
        {
            "id": "multi-step",
            "fromAmount": "1000000",
            "toAmount": "970000",
            "toAmountMin": "960000",
            "gasCosts": [
                {
                    "amount": "13",
                    "amountUSD": "0.05",
                    "priceUSD": "2.3",
                    "token": {
                        "chainId": 10,
                        "address": "0x0000000000000000000000000000000000000000",
                        "decimals": 18,
                    },
                }
            ],
            "feeCosts": [
                {
                    "amount": "3",
                    "amountUSD": "0.06",
                    "priceUSD": "1.1",
                    "token": {
                        "chainId": 8453,
                        "address": DESTINATION_TOKEN,
                        "decimals": 6,
                    },
                }
            ],
            "steps": [first, final],
        }
    )

    assert tuple(step.provider for step in route.evidence.steps) == (
        "arbitrary-swap-provider",
        "across",
    )
    assert tuple(step.source for step in route.evidence.steps) == (
        AssetIdentity(10, SOURCE_TOKEN, 6),
        AssetIdentity(10, intermediate, 18),
    )
    assert route.evidence.destination == AssetIdentity(8453, DESTINATION_TOKEN, 6)
    assert len(route.gas_costs) == 1
    assert route.gas_costs[0].token == AssetIdentity(10, "native", 18)
    assert len(route.fee_costs) == 1
    assert route.fee_costs[0].token == AssetIdentity(8453, DESTINATION_TOKEN, 6)
    quote_prices = [
        (price.asset, price.price_usd)
        for price in route.evidence.prices
        if price.price_usd is not None
    ]
    assert (AssetIdentity(10, SOURCE_TOKEN, 6), "0.4") in quote_prices
    assert (AssetIdentity(10, "0x" + "d" * 40, 8), "0.6") in quote_prices
    assert (AssetIdentity(10, "0x" + "e" * 40, 18), "1.7") in quote_prices
    assert (AssetIdentity(8453, "0x" + "e" * 40, 18), "2.1") in quote_prices
    intermediate_prices = [
        (price.asset, price.price_usd)
        for price in route.evidence.prices
        if price.asset == AssetIdentity(10, intermediate, 18)
    ]
    assert intermediate_prices == [
        (AssetIdentity(10, intermediate, 18), "2.5"),
        (AssetIdentity(10, intermediate, 18), "2.75"),
    ]
    assert any(
        price.asset == AssetIdentity(10, SOURCE_TOKEN, 6)
        and price.price_usd == "1.5"
        for price in route.evidence.prices
    )
    assert any(
        price.asset == AssetIdentity(8453, DESTINATION_TOKEN, 6)
        and price.price_usd == "3.5"
        for price in route.evidence.prices
    )
    assert all(
        price.timestamp == route.evidence.quote_timestamp
        for price in route.evidence.prices
        if price.price_usd in {"1.5", "2.5", "2.75", "3.5"}
    )
    assert validate_bridge_route(
        route,
        expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
        expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
        recipient=WALLET.upper().replace("0X", "0x"),
        input_amount=1_000_000,
    ) is route


def test_route_validation_compares_complete_asset_identities_including_decimals():
    route = _route_from_dict(
        {
            "id": "exact-identities",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "steps": [_bridge_step()],
        }
    )

    with pytest.raises(ValueError, match="source identity"):
        validate_bridge_route(
            route,
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 18),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )


def test_route_rejects_a_multi_step_quote_when_one_step_cannot_be_normalized():
    malformed_step = {"id": "broken", "type": "swap", "tool": "provider"}

    with pytest.raises(ValueError, match="unnormalizable executable step"):
        _route_from_dict(
            {
                "id": "bad-multi-step",
                "fromAmount": "1000000",
                "toAmount": "998000",
                "toAmountMin": "990000",
                "steps": [_bridge_step(), malformed_step],
            }
        )


def test_route_rejects_gas_cost_without_exact_token_identity():
    with pytest.raises(ValueError, match="gas cost token"):
        _route_from_dict(
            {
                "id": "gas-without-identity",
                "fromAmount": "1000000",
                "toAmount": "998000",
                "toAmountMin": "990000",
                "gasCosts": [{"amount": "21000", "amountUSD": "0.03"}],
                "steps": [_bridge_step()],
            }
        )


def test_route_evidence_integrity_rejects_mutated_step_and_replaced_min_output():
    route = _two_step_route()
    route.raw_steps[0]["action"]["toAddress"] = "0x" + "2" * 40

    with pytest.raises(LifiError, match="integrity"):
        validate_bridge_route(
            route,
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )

    clean_route = _two_step_route()
    replaced_route = replace(clean_route, to_amount_min=clean_route.to_amount_min + 1)
    with pytest.raises(LifiError, match="integrity"):
        validate_bridge_route(
            replaced_route,
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )

    replaced_output = replace(clean_route, to_amount=clean_route.to_amount + 1)
    with pytest.raises(LifiError, match="integrity"):
        validate_bridge_route(
            replaced_output,
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )


def test_route_evidence_integrity_rejects_replaced_payload_fingerprint():
    route = _two_step_route()
    replaced_evidence = replace(route.evidence, payload_sha256="0" * 64)

    with pytest.raises(LifiError, match="integrity"):
        validate_bridge_route(
            replace(route, evidence=replaced_evidence),
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )


@pytest.mark.parametrize(
    ("second_token", "second_amount", "message"),
    [
        ("0x" + "d" * 40, "800000000000000000", "identity continuity"),
        ("0x" + "c" * 40, "700000000000000000", "amount continuity"),
    ],
)
def test_route_validation_requires_top_level_step_continuity(
    second_token, second_amount, message
):
    route = _two_step_route(second_token=second_token, second_amount=second_amount)

    with pytest.raises(ValueError, match=message):
        validate_bridge_route(
            route,
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )


def test_route_validation_ties_output_amount_to_last_top_level_step():
    route_payload = json.loads(_two_step_route().evidence.canonical_payload_json)
    route_payload["steps"][-1]["estimate"]["toAmount"] = "700001"
    route = _route_from_dict(route_payload)

    with pytest.raises(ValueError, match="output amount"):
        validate_bridge_route(
            route,
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )


@pytest.mark.parametrize(
    ("field", "step_output"),
    [("toAmount", "700000"), ("toAmountMin", "700000"), ("toAmount", "-1")],
)
def test_lifi_route_parser_rejects_negative_route_or_step_output_amounts(
    field, step_output
):
    route_payload = {
        "id": "negative-output",
        "fromAmount": "1000000",
        "toAmount": "700000",
        "toAmountMin": "690000",
        "steps": [_bridge_step()],
    }
    if field == "toAmountMin":
        route_payload[field] = "-1"
    elif step_output == "-1":
        route_payload["steps"][0]["estimate"]["toAmount"] = step_output
    else:
        route_payload[field] = "-1"

    with pytest.raises(LifiError, match="non-negative"):
        _route_from_dict(route_payload)


def test_lifi_route_parser_rejects_minimum_output_above_quoted_output():
    with pytest.raises(LifiError, match="minimum output"):
        _route_from_dict(
            {
                "id": "minimum-exceeds-output",
                "fromAmount": "1000000",
                "toAmount": "700000",
                "toAmountMin": "700001",
                "steps": [_bridge_step()],
            }
        )


@pytest.mark.parametrize("cost_kind", ["gasCosts", "feeCosts"])
def test_lifi_route_parser_rejects_negative_cost_amounts(cost_kind):
    step = _bridge_step()
    cost = {
        "amount": "-1",
        "amountUSD": "0.03",
        "token": {"chainId": 10, "address": SOURCE_TOKEN, "decimals": 6},
    }
    if cost_kind == "gasCosts":
        step["estimate"][cost_kind] = [cost]
    else:
        step["estimate"][cost_kind] = [cost]

    with pytest.raises(LifiError, match="non-negative"):
        _route_from_dict(
            {
                "id": "negative-cost",
                "fromAmount": "1000000",
                "toAmount": "998000",
                "toAmountMin": "990000",
                "steps": [step],
            }
        )


def test_empty_route_cost_aggregates_fall_back_to_top_level_step_costs():
    step = _bridge_step()
    step["estimate"]["feeCosts"][0]["priceUSD"] = "0.75"
    step["estimate"]["gasCosts"] = [
        {
            "amount": "21000",
            "amountUSD": "0.03",
            "priceUSD": "2500",
            "token": {
                "chainId": 10,
                "address": "0x0000000000000000000000000000000000000000",
                "decimals": 18,
            },
        }
    ]
    route = _route_from_dict(
        {
            "id": "empty-aggregate-fallback",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "gasCosts": [],
            "feeCosts": [],
            "steps": [step],
        }
    )

    assert len(route.gas_costs) == 1
    assert route.gas_costs[0].amount == 21_000
    assert len(route.fee_costs) == 1
    assert route.fee_costs[0].amount == 100


def test_nested_estimate_costs_remain_in_evidence_without_valuation_double_counting():
    parent = _bridge_step()
    parent["estimate"]["feeCosts"][0]["priceUSD"] = "0.75"
    nested = _bridge_step()
    nested["id"] = "nested-step"
    nested["estimate"]["feeCosts"][0].update(
        {
            "amount": "9",
            "priceUSD": "0.95",
            "token": {"chainId": 8453, "address": DESTINATION_TOKEN, "decimals": 6},
        }
    )
    parent["includedSteps"] = [nested]
    route = _route_from_dict(
        {
            "id": "nested-costs",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "feeCosts": parent["estimate"]["feeCosts"],
            "steps": [parent],
        }
    )

    assert len(route.fee_costs) == 1
    assert route.fee_costs[0].amount == 100
    assert any(
        price.asset == AssetIdentity(8453, DESTINATION_TOKEN, 6)
        and price.price_usd == "0.95"
        for price in route.evidence.prices
    )


def test_nested_estimate_costs_without_route_aggregate_are_marked_incomplete():
    parent = _bridge_step()
    nested = _bridge_step()
    nested["id"] = "nested-step"
    nested["estimate"]["feeCosts"][0].update(
        {
            "amount": "9",
            "priceUSD": "0.95",
            "token": {"chainId": 8453, "address": DESTINATION_TOKEN, "decimals": 6},
        }
    )
    parent["includedSteps"] = [nested]

    route = _route_from_dict(
        {
            "id": "nested-costs-without-aggregate",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "steps": [parent],
        }
    )

    assert route.evidence.costs_complete is False
    assert "nested fee costs require a route-level aggregate" in (
        route.evidence.costs_incomplete_reason
    )
    assert len(route.fee_costs) == 1
    assert any(
        price.asset == AssetIdentity(8453, DESTINATION_TOKEN, 6)
        and price.price_usd == "0.95"
        for price in route.evidence.prices
    )
    payload = json.loads(route.evidence.canonical_payload_json)
    assert payload["steps"][0]["includedSteps"][0]["estimate"]["feeCosts"][0][
        "amount"
    ] == "9"


def test_route_level_output_price_and_endpoint_use_final_top_level_step():
    parent = _bridge_step()
    parent["estimate"]["feeCosts"] = []
    nested = _bridge_step()
    nested["id"] = "nested-step"
    nested["estimate"]["feeCosts"] = []
    nested["action"]["toChainId"] = 10
    nested["action"]["toToken"] = {
        "address": "0x" + "d" * 40,
        "decimals": 18,
        "priceUSD": "0.50",
    }
    parent["includedSteps"] = [nested]
    route = _route_from_dict(
        {
            "id": "top-level-output-price",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "toTokenPriceUSD": "1.25",
            "steps": [parent],
        }
    )

    assert route.evidence.destination == AssetIdentity(
        8453, DESTINATION_TOKEN, 6
    )
    assert route.evidence.final_recipient == WALLET.lower()
    assert any(
        price.asset == AssetIdentity(8453, DESTINATION_TOKEN, 6)
        and price.price_usd == "1.25"
        for price in route.evidence.prices
    )


@pytest.mark.parametrize("mismatch", ["identity", "amount", "output"])
def test_route_validation_rejects_nested_steps_that_do_not_match_parent(mismatch):
    parent = _bridge_step()
    parent["estimate"]["toAmount"] = "998000"
    nested = _bridge_step()
    nested["id"] = "nested-step"
    nested["estimate"]["toAmount"] = "998000"
    if mismatch == "identity":
        nested["action"]["toToken"] = {
            "address": "0x" + "d" * 40,
            "decimals": 18,
        }
        nested["action"]["toChainId"] = 10
    elif mismatch == "amount":
        nested["action"]["fromAmount"] = "999999"
    else:
        nested["estimate"]["toAmount"] = "997999"
    parent["includedSteps"] = [nested]
    route = _route_from_dict(
        {
            "id": f"nested-{mismatch}-mismatch",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "feeCosts": parent["estimate"]["feeCosts"],
            "steps": [parent],
        }
    )

    with pytest.raises(ValueError, match="nested step"):
        validate_bridge_route(
            route,
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda route: route.update({"fromAmount": True}), "amount"),
        (lambda route: route.update({"fromAmount": 1_000_000.0}), "amount"),
        (lambda route: route["steps"][0]["action"].update({"fromChainId": True}), "chain"),
        (
            lambda route: route["steps"][0]["action"].update({"fromChainId": 10.0}),
            "chain",
        ),
    ],
)
def test_lifi_route_parser_rejects_bool_or_float_amounts_and_chain_ids(mutate, message):
    route_payload = {
        "id": "strict-integers",
        "fromAmount": "1000000",
        "toAmount": "998000",
        "toAmountMin": "990000",
        "steps": [_bridge_step()],
    }
    mutate(route_payload)

    with pytest.raises(LifiError, match=message):
        _route_from_dict(route_payload)


def test_lifi_fee_cost_requires_token_object_and_raises_lifi_error():
    step = _bridge_step()
    step["estimate"]["feeCosts"][0]["token"] = "not-a-token-object"

    with pytest.raises(LifiError, match="fee cost token"):
        _route_from_dict(
            {
                "id": "malformed-fee-token",
                "fromAmount": "1000000",
                "toAmount": "998000",
                "toAmountMin": "990000",
                "steps": [step],
            }
        )


@pytest.mark.parametrize("recipient", [None, "0x" + "2" * 40])
def test_route_validation_requires_matching_final_transfer_recipient(recipient):
    step = _bridge_step()
    if recipient is None:
        del step["action"]["toAddress"]
    else:
        step["action"]["toAddress"] = recipient
    route = _route_from_dict(
        {
            "id": "route-recipient",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "steps": [step],
        }
    )

    with pytest.raises(ValueError, match="recipient"):
        validate_bridge_route(
            route,
            expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
            expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
            recipient=WALLET,
            input_amount=1_000_000,
        )


@pytest.mark.parametrize(
    ("source_price", "timestamp", "reason"),
    [
        (None, "2026-09-22T12:00:00Z", "missing_source_price"),
        ("0", "2026-09-22T12:00:00Z", "invalid_price_evidence"),
        ("1", "2020-01-01T00:00:00Z", "stale_price"),
    ],
)
def test_lifi_candidate_rejects_missing_non_positive_or_stale_route_prices(
    source_price, timestamp, reason
):
    step = _bridge_step()
    step["estimate"]["feeCosts"] = []

    route = _route_from_dict(
        {
            "id": "priced-route",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "gasCosts": [
                {
                    "amount": "1",
                    "priceUSD": "2000",
                    "token": {
                        "chainId": 10,
                        "address": "0x0000000000000000000000000000000000000000",
                        "decimals": 18,
                    },
                }
            ],
            "fromTokenPriceUSD": source_price,
            "toTokenPriceUSD": "1",
            "priceTimestamp": timestamp,
            "steps": [step],
        }
    )
    candidate = candidate_from_route(
        route,
        expected_source=AssetIdentity(10, SOURCE_TOKEN, 6),
        expected_destination=AssetIdentity(8453, DESTINATION_TOKEN, 6),
        expected_input_amount=1_000_000,
        recipient=WALLET,
        gas_estimate_complete=True,
        now=QUOTE_NOW,
        max_price_age=timedelta(minutes=5),
    )
    assert candidate.reason == reason


@pytest.mark.parametrize("untrusted_price", ["0", "not-a-price"])
def test_route_parser_preserves_untrusted_price_evidence_for_candidate_review(
    untrusted_price,
):
    route = _route_from_dict(
        {
            "id": "untrusted-price-route",
            "fromAmount": "1000000",
            "toAmount": "998000",
            "toAmountMin": "990000",
            "fromTokenPriceUSD": untrusted_price,
            "toTokenPriceUSD": "1",
            "priceTimestamp": "2026-09-22T12:00:00Z",
            "steps": [_bridge_step()],
        }
    )

    assert any(
        price.asset == AssetIdentity(10, SOURCE_TOKEN, 6)
        and price.price_usd == untrusted_price
        for price in route.evidence.prices
    )


def test_payload_fingerprint_is_stable_across_object_key_order():
    payload = {
        "id": "route-fingerprint",
        "fromAmount": "1000000",
        "toAmount": "998000",
        "toAmountMin": "990000",
        "steps": [_bridge_step()],
    }
    reordered = {
        "steps": [
            {
                "estimate": payload["steps"][0]["estimate"],
                "action": payload["steps"][0]["action"],
                "tool": "across",
                "type": "cross",
                "id": "step-1",
            }
        ],
        "toAmountMin": "990000",
        "toAmount": "998000",
        "fromAmount": "1000000",
        "id": "route-fingerprint",
    }

    assert _route_from_dict(payload).evidence.payload_sha256 == _route_from_dict(
        reordered
    ).evidence.payload_sha256


@pytest.mark.parametrize(
    ("asset", "token_query"),
    [
        (AssetIdentity(10, SOURCE_TOKEN, 6), SOURCE_TOKEN),
        (
            AssetIdentity(10, "native", 18),
            "0x0000000000000000000000000000000000000000",
        ),
    ],
)
def test_exact_token_price_uses_chain_and_exact_address_and_timestamps_snapshot(
    asset, token_query
):
    class PriceResponse(Response):
        def json(self):
            return {
                "address": token_query,
                "chainId": 10,
                "decimals": asset.decimals,
                "priceUSD": "123.45",
            }

    class PriceHttp(HttpClient):
        def get(self, url, *, params, headers, timeout):
            self.calls.append((url, params, headers, timeout))
            return PriceResponse()

    http = PriceHttp()
    price = LifiClient(http).token_price(asset)

    assert http.calls[0][0] == "https://li.quest/v1/token"
    assert http.calls[0][1] == {"chain": 10, "token": token_query}
    assert price.asset == asset
    assert price.price_usd == "123.45"
    assert datetime.fromisoformat(price.timestamp.replace("Z", "+00:00")).tzinfo == UTC


@pytest.mark.parametrize("untrusted_price", ["0", "not-a-price"])
def test_exact_token_price_endpoint_still_rejects_invalid_values(untrusted_price):
    asset = AssetIdentity(10, SOURCE_TOKEN, 6)

    class InvalidPriceResponse(Response):
        def json(self):
            return {
                "address": SOURCE_TOKEN,
                "chainId": 10,
                "decimals": 6,
                "priceUSD": untrusted_price,
            }

    class InvalidPriceHttp(HttpClient):
        def get(self, url, *, params, headers, timeout):
            return InvalidPriceResponse()

    with pytest.raises(ValueError, match="price"):
        LifiClient(InvalidPriceHttp()).token_price(asset)


def test_missing_native_gas_price_is_fetched_by_exact_token_identity():
    route_body = {
        "id": "native-gas",
        "fromAmount": "1000000",
        "toAmount": "998000",
        "toAmountMin": "990000",
        "gasCosts": [
            {
                "amount": "100",
                "token": {
                    "chainId": 10,
                    "address": "0x0000000000000000000000000000000000000000",
                    "decimals": 18,
                },
            }
        ],
        "steps": [_bridge_step()],
    }

    class GasRouteResponse(Response):
        def json(self):
            return {"routes": [route_body]}

    class GasPriceHttp(HttpClient):
        def post(self, url, *, json, headers, timeout):
            self.calls.append((url, json, headers, timeout))
            return GasRouteResponse()

        def get(self, url, *, params, headers, timeout):
            self.calls.append((url, params, headers, timeout))
            return type(
                "GasPriceResponse",
                (Response,),
                {
                    "json": lambda self: {
                        "address": "0x0000000000000000000000000000000000000000",
                        "chainId": 10,
                        "decimals": 18,
                        "priceUSD": "2500",
                    }
                },
            )()

    http = GasPriceHttp()
    client = LifiClient(http)
    route = client.routes(
        LifiRouteRequest(10, 8453, SOURCE_TOKEN, DESTINATION_TOKEN, "1000000", WALLET, WALLET)
    )[0]

    price_call = next(call for call in http.calls if call[0] == "https://li.quest/v1/token")
    assert price_call[1] == {
        "chain": 10,
        "token": "0x0000000000000000000000000000000000000000",
    }
    assert route.gas_costs[0].price_usd == "2500"
    assert route.gas_costs[0].price_timestamp is not None
    assert any(
        price.asset == AssetIdentity(10, "native", 18)
        and price.price_usd == "2500"
        for price in route.evidence.prices
    )
