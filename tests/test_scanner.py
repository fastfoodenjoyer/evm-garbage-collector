from evm_inventory.config import load_catalog, snapshot
from evm_inventory.scanner import _rpc_urls, scan
from evm_inventory.store import Store

W = "0x" + "1" * 40
T = "0x" + "2" * 40


def scope():
    return {
        "wallets": [W],
        "settings": {"delay_min": 0, "delay_max": 0},
        "catalog": {
            "networks": [
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
            ]
        },
    }


class RPC:
    def block(self, url, chain_id, number=None):
        return {"number": 1, "hash": "0x" + "a" * 64, "timestamp": 1}

    def native(self, *args):
        return 0

    def token(self, *args):
        return 1230000, 6


def test_zero_native_still_checks_stable_resume_skips(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    run = store.create_run(scope())
    result = scan(store, run, rpc=RPC(), sleep=lambda _: None)
    assert result["status"] == "completed"
    jobs = store.jobs(run)
    assert next(j for j in jobs if j["asset_id"] == T)["result"]["raw_balance"] == "1230000"
    assert next(j for j in jobs if j["asset_id"] == "native")["result"]["raw_balance"] == "0"
    result = scan(store, run, rpc=RPC(), sleep=lambda _: None)
    assert all(j["attempts"] == 1 for j in store.jobs(run) if j["kind"] == "mandatory")
    store.close()


def test_bnb_and_polygon_create_mandatory_jobs_for_each_wallet(tmp_path):
    catalog = load_catalog()
    selected = tuple(network for network in catalog.networks if network.chain_id in {56, 137})
    assert {network.chain_id for network in selected} == {56, 137}
    two_wallets = (W, "0x" + "4" * 40)
    scope = snapshot(
        catalog.__class__(
            networks=selected,
            revision=catalog.revision,
            checked_at=catalog.checked_at,
            source=catalog.source,
        ),
        two_wallets,
        {"delay_min": 0, "delay_max": 0},
    )

    with Store(tmp_path / "db.sqlite") as store:
        run = store.create_run(scope)
        assert scan(store, run, rpc=RPC(), sleep=lambda _: None)["status"] == "completed"
        mandatory = [job for job in store.jobs(run) if job["kind"] == "mandatory"]

    expected_assets = {
        network.chain_id: {"native", *(token.address for token in network.tokens)}
        for network in selected
    }
    assert len(mandatory) == len(two_wallets) * sum(map(len, expected_assets.values()))
    for wallet in two_wallets:
        for chain_id, assets in expected_assets.items():
            assert {
                job["asset_id"]
                for job in mandatory
                if job["wallet"] == wallet and job["chain_id"] == chain_id
            } == assets


def test_network_failure_creates_all_checks(tmp_path):
    s = scope()
    s["catalog"]["networks"][0]["rpc_urls"] = []
    store = Store(tmp_path / "db.sqlite")
    run = store.create_run(s)
    assert scan(store, run, rpc=RPC())["status"] == "incomplete"
    jobs = [j for j in store.jobs(run) if j["kind"] == "mandatory"]
    assert len(jobs) == 2 and all(j["status"] == "unavailable" for j in jobs)
    store.close()


def test_rpc_urls_accepts_the_documented_alchemy_api_key(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "test-key")
    network = {"rpc_urls": ["https://public.example"], "alchemy_network": "opt-mainnet"}

    urls = _rpc_urls(network)

    assert urls[0] == "https://opt-mainnet.g.alchemy.com/v2/test-key"


class Discover:
    enabled = True
    transport = object()
    calls = 0

    def page(self, wallet, network, cursor=None):
        self.calls += 1
        return [
            {
                "address": "0x" + "3" * 40,
                "symbol": "OTHER",
                "decimals": 6,
                "reported_raw_balance": "1230000",
            }
        ], None


def test_discovery_runs_when_rpc_unavailable(tmp_path):
    s = scope()
    n = s["catalog"]["networks"][0]
    n["rpc_urls"] = []
    n["alchemy_network"] = "opt-mainnet"
    with Store(tmp_path / "db.sqlite") as st:
        run = st.create_run(s)
        d = Discover()
        summary = scan(st, run, rpc=RPC(), discovery=d)
        assert d.calls == 1
        assert summary["status"] == "incomplete"
        jobs = st.jobs(run)
        assert next(j for j in jobs if j["kind"] == "discovery")["status"] == "success"
        discovered = next(j for j in jobs if j["kind"] == "discovered")
        assert discovered["status"] == "provider_only"
        assert discovered["result"]["amount"] == "1.230000"


def test_failed_token_not_retried_twice_in_same_pass(tmp_path):
    from evm_inventory.transport import RequestError

    class BadToken(RPC):
        calls = 0

        def token(self, *args):
            self.calls += 1
            raise RequestError("rpc_error")

    with Store(tmp_path / "db.sqlite") as st:
        run = st.create_run(scope())
        rpc = BadToken()
        scan(st, run, rpc=rpc)
        assert rpc.calls == 1


def test_unavailable_rpc_has_cooldown_across_wallets(tmp_path):
    from evm_inventory.transport import RequestError

    s = scope()
    s["wallets"].append("0x" + "4" * 40)

    class BadRPC(RPC):
        calls = 0

        def block(self, *args):
            self.calls += 1
            raise RequestError("network_error")

    with Store(tmp_path / "db.sqlite") as st:
        run = st.create_run(s)
        rpc = BadRPC()
        scan(st, run, rpc=rpc, sleep=lambda _: None)
        assert rpc.calls == 1


def test_alchemy_rpc_is_used_without_public_fallback(monkeypatch):
    monkeypatch.setenv("ALCHEMY_RPC_API_KEY", "test-key")
    urls = _rpc_urls({"alchemy_network": "opt-mainnet", "rpc_urls": ["https://rpc.example"]})
    assert urls == ("https://opt-mainnet.g.alchemy.com/v2/test-key",)


def test_rpc_urls_are_resolved_once_for_all_networks(monkeypatch, tmp_path):
    monkeypatch.setenv("ALCHEMY_API_KEY", "test-key")
    calls = []
    original = __import__("evm_inventory.scanner", fromlist=["_rpc_urls"])._rpc_urls

    def counted(network):
        calls.append(network["chain_id"])
        return original(network)

    monkeypatch.setattr("evm_inventory.scanner._rpc_urls", counted)
    with Store(tmp_path / "db.sqlite") as store:
        run = store.create_run(scope())
        scan(store, run, rpc=RPC(), sleep=lambda _: None)
    assert calls == [10]


def test_rpc_failure_during_balances_defers_remaining_calls(tmp_path):
    from evm_inventory.transport import RequestError

    class FailedBalance(RPC):
        native_calls = 0
        token_calls = 0

        def native(self, *args):
            self.native_calls += 1
            raise RequestError("network_error")

        def token(self, *args):
            self.token_calls += 1
            return 0, 6

    with Store(tmp_path / "db.sqlite") as store:
        run = store.create_run(scope())
        rpc = FailedBalance()
        scan(store, run, rpc=rpc)
        assert rpc.native_calls == 1
        assert rpc.token_calls == 0
        assert all(j["status"] == "deferred" for j in store.jobs(run) if j["kind"] == "mandatory")


def test_resume_discovery_keeps_cursor_and_clears_previous_error(tmp_path):
    from evm_inventory.transport import RequestError

    s = scope()
    s["catalog"]["networks"][0]["alchemy_network"] = "opt-mainnet"

    class Pages(Discover):
        cursors = []
        fail = True

        def page(self, wallet, network, cursor=None):
            self.cursors.append(cursor)
            if cursor is None:
                return [{"address": "0x" + "3" * 40, "symbol": "TOKEN", "decimals": 6}], "page2"
            if self.fail:
                self.fail = False
                raise RequestError("discovery_partial")
            return [], None

    with Store(tmp_path / "db.sqlite") as store:
        run = store.create_run(s)
        discovery = Pages()
        scan(store, run, rpc=RPC(), discovery=discovery)
        scan(store, run, rpc=RPC(), discovery=discovery, resume=True)
        assert discovery.cursors == [None, "page2", "page2"]
        job = next(j for j in store.jobs(run) if j["kind"] == "discovery")
        assert job["status"] == "success"
        assert "error" not in job["result"]
        assert job["retry_after"] is None
