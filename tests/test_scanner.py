from evm_inventory.scanner import _rpc_urls, scan
from evm_inventory.store import Store

W = "0x" + "A" * 40
T = "0x" + "B" * 40
D = "0x" + "C" * 40


def scope(*, wallets=None, networks=None):
    return {
        "wallets": wallets or [W],
        "settings": {"delay_min": 0, "delay_max": 0},
        "catalog": {
            "networks": networks
            or [
                {
                    "chain_id": 10,
                    "name": "OP",
                    "rpc_urls": ["https://rpc.test"],
                    "native_symbol": "ETH",
                    "native_decimals": 18,
                    "token_review_status": "verified",
                    "alchemy_network": None,
                    "tokens": [
                        {
                            "address": T,
                            "symbol": "USDC",
                            "decimals": 6,
                            "source": "https://example.com",
                            "checked_at": "2026-09-19",
                            "variant": "",
                        }
                    ],
                }
            ],
        },
    }


class RPC:
    native_calls = 0
    token_calls = 0

    def block(self, url, chain_id, number=None):
        return {"number": 1, "hash": "0x" + "a" * 64, "timestamp": 1}

    def native(self, *args):
        self.native_calls += 1
        return 0

    def token(self, *args):
        self.token_calls += 1
        return 1230000, 6


class Discover:
    enabled = True

    def __init__(self, pages=None):
        self.pages = pages or [([], None)]
        self.calls = []

    def page(self, wallet, network, cursor=None):
        self.calls.append(cursor)
        return self.pages.pop(0)


def test_scan_updates_current_asset_and_pass_without_creating_a_second_row(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        first = scan(store, scope(), rpc=RPC(), sleep=lambda _: None)
        asset = next(row for row in store.assets() if row["asset_id"] == T.lower())
        old_created, old_modified = asset["created_at"], asset["modified_at"]
        old_pass_modified = store.passes()[0]["modified_at"]
        second = scan(store, scope(), rpc=RPC(), sleep=lambda _: None)
        updated = next(row for row in store.assets() if row["asset_id"] == T.lower())
        assert first["mandatory_total"] == second["mandatory_total"] == 2
        assert "run_id" not in second and "status" not in second
        assert len(store.assets()) == 2
        assert updated["created_at"] == old_created
        assert updated["modified_at"] > old_modified
        assert updated["status"] == "success"
        assert store.passes()[0]["modified_at"] > old_pass_modified


def test_mandatory_metadata_is_refreshed_with_network_coverage_context(tmp_path):
    first, second = scope(), scope()
    second["catalog"]["networks"][0].update(name="Optimism", token_review_status="pending")
    second["catalog"]["networks"][0]["tokens"][0]["symbol"] = "USDC.e"
    with Store(tmp_path / "db.sqlite") as store:
        scan(store, first, rpc=RPC(), sleep=lambda _: None)
        scan(store, second, rpc=RPC(), sleep=lambda _: None)
        token = next(row for row in store.assets() if row["asset_id"] == T.lower())
    assert token["metadata"].get("symbol") == "USDC.e"
    assert token["metadata"].get("network_name") == "Optimism"
    assert token["metadata"].get("token_review_status") == "pending"


def test_discovery_state_restarts_each_scan_and_persists_page_progress(tmp_path):
    s = scope()
    s["catalog"]["networks"][0]["alchemy_network"] = "opt-mainnet"
    pages = Discover([([], "page-2"), ([], None), ([], None)])
    with Store(tmp_path / "db.sqlite") as store:
        scan(store, s, rpc=RPC(), discovery=pages, sleep=lambda _: None)
        first = store.discovery_state(W, 10)
        scan(store, s, rpc=RPC(), discovery=pages, sleep=lambda _: None)
        second = store.discovery_state(W, 10)
    assert pages.calls == [None, "page-2", None]
    assert first["status"] == second["status"] == "success"
    assert first["cursor_history"] == ["page-2"]
    assert second["cursor"] is None and second["cursor_history"] == []
    assert second["created_at"] == first["created_at"]
    assert second["retry_after"] is None and second["error"] is None


def test_discovery_error_persists_cursor_retry_and_error_for_its_wallet_network(tmp_path):
    from evm_inventory.transport import RequestError

    s = scope()
    s["catalog"]["networks"][0]["alchemy_network"] = "opt-mainnet"

    class PartialDiscovery(Discover):
        def page(self, wallet, network, cursor=None):
            self.calls.append(cursor)
            if cursor is None:
                return [], "page-2"
            raise RequestError("rate_limited", retry_after=123.0)

    discovery = PartialDiscovery()
    with Store(tmp_path / "db.sqlite") as store:
        scan(store, s, rpc=RPC(), discovery=discovery, sleep=lambda _: None)
        state = store.discovery_state(W, 10)
    assert discovery.calls == [None, "page-2"]
    assert state["cursor"] == "page-2"
    assert state["cursor_history"] == ["page-2"]
    assert state["status"] == "deferred"
    assert state["retry_after"] == 123.0
    assert state["error"]["code"] == "rate_limited"


def test_discovery_runs_despite_unavailable_rpc_and_retains_unreturned_assets(tmp_path):
    s = scope()
    network = s["catalog"]["networks"][0]
    network.update(rpc_urls=[], alchemy_network="opt-mainnet")
    discovery = Discover(
        [
            (
                [{"address": D, "symbol": "OTHER", "decimals": 6, "reported_raw_balance": "10"}],
                None,
            ),
            ([], None),
        ]
    )
    with Store(tmp_path / "db.sqlite") as store:
        scan(store, s, rpc=RPC(), discovery=discovery, sleep=lambda _: None)
        discovered = next(row for row in store.assets() if row["asset_id"] == D.lower())
        scan(store, s, rpc=RPC(), discovery=discovery, sleep=lambda _: None)
        retained = next(row for row in store.assets() if row["asset_id"] == D.lower())
    assert discovered["status"] == "provider_only"
    assert retained == discovered


def test_discovered_asset_becomes_curated_and_rpc_replaces_provider_observation(tmp_path):
    first = scope()
    first_network = first["catalog"]["networks"][0]
    first_network.update(tokens=[], alchemy_network="opt-mainnet")
    discovery = Discover(
        [
            (
                [
                    {
                        "address": T,
                        "symbol": "unreviewed",
                        "decimals": 18,
                        "reported_raw_balance": "999",
                    }
                ],
                None,
            )
        ]
    )
    second = scope()
    rpc = RPC()
    with Store(tmp_path / "db.sqlite") as store:
        scan(store, first, rpc=rpc, discovery=discovery, sleep=lambda _: None)
        provider_row = next(row for row in store.assets() if row["asset_id"] == T.lower())
        scan(store, second, rpc=rpc, discovery=Discover(), sleep=lambda _: None)
        curated_row = next(row for row in store.assets() if row["asset_id"] == T.lower())
    assert provider_row["kind"] == "discovered"
    assert provider_row["result"]["source"] == "alchemy"
    assert curated_row["kind"] == "mandatory"
    assert curated_row["metadata"]["symbol"] == "USDC"
    assert curated_row["metadata"]["network_name"] == "OP"
    assert curated_row["status"] == "success"
    assert curated_row["result"]["raw_balance"] == "1230000"
    assert curated_row["result"]["source"] == "rpc"


def test_curated_discovery_candidate_remains_mandatory_and_is_checked_by_rpc(tmp_path):
    s = scope()
    s["catalog"]["networks"][0]["alchemy_network"] = "opt-mainnet"
    discovery = Discover(
        [([{"address": T, "symbol": "wrong", "decimals": 18, "reported_raw_balance": "999"}], None)]
    )
    rpc = RPC()
    with Store(tmp_path / "db.sqlite") as store:
        scan(store, s, rpc=rpc, discovery=discovery, sleep=lambda _: None)
        token = next(row for row in store.assets() if row["asset_id"] == T.lower())
    assert rpc.token_calls == 1
    assert token["kind"] == "mandatory"
    assert token["status"] == "success"
    assert token["metadata"]["symbol"] == "USDC"
    assert token["result"]["source"] == "rpc"


def test_assets_outside_later_scope_are_retained_unchanged(tmp_path):
    other_wallet = "0x" + "D" * 40
    later_network = {
        **scope()["catalog"]["networks"][0],
        "chain_id": 137,
        "name": "Polygon",
        "tokens": [],
    }
    with Store(tmp_path / "db.sqlite") as store:
        scan(
            store,
            scope(
                wallets=[W, other_wallet],
                networks=[scope()["catalog"]["networks"][0], later_network],
            ),
            rpc=RPC(),
            sleep=lambda _: None,
        )
        before = [
            row
            for row in store.assets()
            if row["wallet"] == other_wallet.lower() or row["chain_id"] == 137
        ]
        scan(store, scope(), rpc=RPC(), sleep=lambda _: None)
        after = [
            row
            for row in store.assets()
            if row["wallet"] == other_wallet.lower() or row["chain_id"] == 137
        ]
    assert after == before
    assert all("network_name" in row["metadata"] for row in after)


def test_unavailable_rpc_cooldown_is_shared_across_wallets(tmp_path):
    from evm_inventory.transport import RequestError

    class BadRPC(RPC):
        calls = 0

        def block(self, *args):
            self.calls += 1
            raise RequestError("network_error")

    rpc = BadRPC()
    with Store(tmp_path / "db.sqlite") as store:
        scan(store, scope(wallets=[W, "0x" + "E" * 40]), rpc=rpc, sleep=lambda _: None)
        assert rpc.calls == 1
        assert all(row["status"] in {"unavailable", "deferred"} for row in store.assets())


def test_rpc_urls_accepts_the_documented_alchemy_api_key(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "test-key")
    urls = _rpc_urls({"rpc_urls": ["https://public.example"], "alchemy_network": "opt-mainnet"})
    assert urls == ("https://opt-mainnet.g.alchemy.com/v2/test-key",)
