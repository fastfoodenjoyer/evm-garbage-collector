from evm_inventory.lifi import LifiClient, LifiRouteRequest


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
                    "gasCosts": [{"amount": "21000", "amountUSD": "0.03"}],
                    "steps": [
                        {"tool": "across", "id": "step-1"},
                        {"tool": "sushiswap"},
                    ],
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
        from_token_address="0x0000000000000000000000000000000000000000",
        to_token_address="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        from_amount="1000000",
        from_address="0x" + "1" * 40,
        to_address="0x" + "1" * 40,
    )

    routes = client.routes(request)

    assert http.calls[0][0] == "https://api.jumper.xyz/pipeline/v1/advanced/routes"
    assert http.calls[0][1]["fromAddress"] == request.from_address
    assert http.calls[0][1]["options"]["order"] == "CHEAPEST"
    assert routes[0].to_amount_min == 990000
    assert routes[0].gas_costs[0].amount == 21000
    assert routes[0].tools == ("across", "sushiswap")


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
