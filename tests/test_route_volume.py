from urllib.parse import parse_qs

import httpx
from test_live_plan import NOW, WALLET, _allowlist, _balance_rows, _RecordingQuotes

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.lifi import LifiClient
from evm_inventory.lifi_http import LifiHttpClient
from evm_inventory.live_plan import create_live_plan


def volume_plan(tmp_path, count, client):
    rows = [
        dict(
            wallet=WALLET,
            chain_id=i + 100,
            asset_id="0x" + "a" * 40,
            raw_balance="10000000",
            decimals=6,
            symbol="USDC",
            status="success",
        )
        for i in range(count)
    ]
    return create_live_plan(
        _balance_rows(tmp_path, rows),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(r["chain_id"], r["asset_id"], "swap") for r in rows]),
        targets=tuple(
            BitgetDepositTarget("USDC", c, "0x" + "b" * 40, 1, decimals=6)
            for c in (10, 8453, 42161)
        ),
        client=client,
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )


def test_twenty_positions_only_sixty_configured_pairs(tmp_path):
    client = _RecordingQuotes(lambda req: ())
    plan = volume_plan(tmp_path, 20, client)
    assert len(client.requests) == 60
    assert len({(r.from_chain_id, r.to_chain_id) for r in client.requests}) == 60
    assert len(plan["entries"]) == 20


def test_thirty_positions_stop_at_real_quota_without_429(tmp_path):
    clock = [NOW.timestamp()]
    routes = []

    def handler(req):
        if req.url.path.endswith("/chains"):
            return httpx.Response(
                200,
                json={
                    "chains": [
                        {"id": c, "chainType": "EVM"} for c in (*range(100, 130), 10, 8453, 42161)
                    ]
                },
            )
        if req.url.path.endswith("/token"):
            params = parse_qs(req.url.query.decode())
            return httpx.Response(
                200,
                json={
                    "address": params["token"][0],
                    "chainId": int(params["chain"][0]),
                    "decimals": 6,
                    "priceUSD": "1",
                },
            )
        routes.append(req)
        return httpx.Response(200, json={"routes": []})

    http = LifiHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        db_path=tmp_path / "inventory.sqlite",
        proxy="http://localhost:1234",
        clock=lambda: clock[0],
        sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
    )
    client = LifiClient(http)
    try:
        plan = volume_plan(tmp_path, 30, client)
        assert len(routes) == 60
        assert http.metrics["provider_429"] == 0
        assert any(
            e.get("reason") == "quota_wait" or e.get("group", {}).get("reason") == "quota_wait"
            for e in plan["entries"]
        )
        assert any(e.get("search", {}).get("complete") is True for e in plan["entries"])
    finally:
        http.close()
