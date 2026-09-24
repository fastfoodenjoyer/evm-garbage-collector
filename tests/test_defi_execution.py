import hashlib
import json

import pytest

from evm_inventory.defi_execution import (
    _action_lock,
    _withdrawal_result,
    execute_defi_action,
    prepare_defi_action,
)
from evm_inventory.defi_plan import create_defi_plan
from evm_inventory.journal import Journal
from evm_inventory.store import Store
from evm_inventory.workbook import WalletWorkbookRow

WALLET = "0x" + "1" * 40
ROUTER = "0x" + "2" * 40
TOKEN = "0x" + "3" * 40


class Rabby:
    def __init__(self, *, amount="12", approval=False):
        self.amount = amount
        self.approval = approval

    def chain_ids(self):
        return {"base": 8453}

    def positions(self, wallet):
        assert wallet == WALLET
        return [
            {
                "id": "vault",
                "chain": "base",
                "portfolio_item_list": [
                    {
                        "name": "Deposit",
                        "pool": {"id": "pool", "chain": "base"},
                        "position_index": "",
                        "proxy_detail": {},
                        "stats": {"net_usd_value": 12, "debt_usd_value": 0},
                        "detail": {"supply_token_list": [{"id": TOKEN, "amount": 12}]},
                        "withdraw_actions": [
                            {
                                "type": "withdraw",
                                "contract_id": ROUTER,
                                "func": "withdraw(uint256,address)",
                                "str_params": [self.amount, WALLET],
                                "need_approve": (
                                    {"token_id": TOKEN, "to": ROUTER, "str_raw_amount": "12"}
                                    if self.approval else {}
                                ),
                            }
                        ],
                    }
                ],
            }
        ]


class Rpc:
    def __init__(self):
        self.calls = []

    def call(self, url, method, params):
        self.calls.append(method)
        if (method == "eth_call"
                and params[0].get("to") == "0x420000000000000000000000000000000000000f"):
            return "0x" + f"{1:064x}"
        return {
            "eth_chainId": "0x2105",
            "eth_gasPrice": "0x1",
            "eth_estimateGas": "0x186a0",
            "eth_getBlockByNumber": {"number": "0x1"},
            "eth_call": "0x",
            "eth_getBalance": "0x100000000",
        }[method]


def sample_plan():
    plan = create_defi_plan([WALLET], Rabby(), supported_chain_ids={8453}, now_seconds=100)
    raw = json.dumps(plan).encode()
    return raw, hashlib.sha256(raw).hexdigest(), plan["entries"][0]["action_id"]


def test_preflight_checks_reviewed_digest_and_exact_action():
    raw, digest, action_id = sample_plan()
    rpc = Rpc()
    prepared = prepare_defi_action(
        raw,
        plan_sha256=digest,
        action_id=action_id,
        wallet=WALLET,
        rabby=Rabby(),
        rpc=rpc,
        rpc_url="https://rpc.test",
        max_gas_wei=1_000_000,
        now_seconds=101,
    )
    assert prepared.request.chain_id == 8453
    assert prepared.request.to == ROUTER
    assert prepared.entry["output_token_ids"] == [TOKEN]
    assert "eth_estimateGas" in rpc.calls
    assert "eth_sendRawTransaction" not in rpc.calls


def test_preflight_rejects_wrong_digest_before_rabby_or_rpc():
    raw, _, action_id = sample_plan()
    with pytest.raises(ValueError, match="digest"):
        prepare_defi_action(
            raw,
            plan_sha256="0" * 64,
            action_id=action_id,
            wallet=WALLET,
            rabby=Rabby(),
            rpc=Rpc(),
            rpc_url="https://rpc.test",
            max_gas_wei=1_000_000,
            now_seconds=101,
        )


def test_preflight_rejects_changed_rabby_action_and_stale_plan():
    raw, digest, action_id = sample_plan()
    with pytest.raises(ValueError, match="changed"):
        prepare_defi_action(
            raw,
            plan_sha256=digest,
            action_id=action_id,
            wallet=WALLET,
            rabby=Rabby(amount="13"),
            rpc=Rpc(),
            rpc_url="https://rpc.test",
            max_gas_wei=1_000_000,
            now_seconds=101,
        )
    with pytest.raises(ValueError, match="stale"):
        prepare_defi_action(
            raw,
            plan_sha256=digest,
            action_id=action_id,
            wallet=WALLET,
            rabby=Rabby(),
            rpc=Rpc(),
            rpc_url="https://rpc.test",
            max_gas_wei=1_000_000,
            now_seconds=1001,
        )


def test_preflight_rejects_gas_cap_and_chain_mismatch():
    raw, digest, action_id = sample_plan()
    with pytest.raises(ValueError, match="gas cap"):
        prepare_defi_action(
            raw,
            plan_sha256=digest,
            action_id=action_id,
            wallet=WALLET,
            rabby=Rabby(),
            rpc=Rpc(),
            rpc_url="https://rpc.test",
            max_gas_wei=1,
            now_seconds=101,
        )
    rpc = Rpc()
    rpc.call = lambda url, method, params: "0x1" if method == "eth_chainId" else "0x1"
    with pytest.raises(ValueError, match="chain"):
        prepare_defi_action(
            raw,
            plan_sha256=digest,
            action_id=action_id,
            wallet=WALLET,
            rabby=Rabby(),
            rpc=rpc,
            rpc_url="https://rpc.test",
            max_gas_wei=1_000_000,
            now_seconds=101,
        )


def test_execute_command_without_flag_only_previews_and_creates_no_journal(tmp_path):
    raw, digest, action_id = sample_plan()
    rpc = Rpc()
    wallet_row = WalletWorkbookRow(2, 1, WALLET, "0x" + "a" * 64, "")
    journal = tmp_path / "defi.sqlite"
    result = execute_defi_action(
        raw,
        plan_sha256=digest,
        action_id=action_id,
        wallet_row=wallet_row,
        rabby=Rabby(),
        rpc=rpc,
        rpc_url="https://rpc.test",
        journal_path=journal,
        max_gas_wei=1_000_000,
        execute=False,
        now_seconds=101,
    )
    assert result["status"] == "preview"
    assert result["action_id"] == action_id
    assert not journal.exists()
    assert "eth_sendRawTransaction" not in rpc.calls


def test_withdrawal_requiring_approval_is_a_separate_stage():
    class ApprovalRpc(Rpc):
        def call(self, url, method, params):
            if method == "eth_call" and params[0]["data"].startswith("0xdd62ed3e"):
                return "0x" + "0" * 64
            return super().call(url, method, params)

    rabby = Rabby(approval=True)
    plan = create_defi_plan([WALLET], rabby, supported_chain_ids={8453}, now_seconds=100)
    raw = json.dumps(plan).encode()
    digest = hashlib.sha256(raw).hexdigest()
    action_id = plan["entries"][0]["action_id"]
    with pytest.raises(ValueError, match="separate approval"):
        prepare_defi_action(
            raw, plan_sha256=digest, action_id=action_id, wallet=WALLET,
            rabby=rabby, rpc=ApprovalRpc(), rpc_url="https://rpc.test",
            max_gas_wei=1_000_000, now_seconds=101,
        )


def test_multiple_rabby_outputs_require_matching_receipt_and_balance_gains():
    from evm_inventory.defi_execution import _TRANSFER_TOPIC

    other = "0x" + "4" * 40
    action = {
        "func": "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)",
        "str_params": [TOKEN, other, "100", "5", "7", WALLET, "9999"],
    }
    entry = {"wallet": WALLET, "action": action, "output_token_ids": [TOKEN, other]}
    recipient = "0x" + WALLET[2:].rjust(64, "0")

    def transfer(token, amount, to=recipient):
        return {
            "address": token,
            "topics": [_TRANSFER_TOPIC, "0x" + "0" * 64, to],
            "data": "0x" + f"{amount:064x}",
        }

    class BalanceRpc:
        def call(self, url, method, params):
            assert method == "eth_call"
            if params[0]["data"] in {"0x313ce567", "0x95d89b41"}:
                return "0x"
            amounts = {TOKEN: 5, other: 7}
            return "0x" + f"{(amounts[params[0]['to']] if params[1] == '0x64' else 0):064x}"

    receipt = {"status": "0x1", "blockNumber": "0x64",
               "logs": [transfer(TOKEN, 5), transfer(other, 7)]}
    rpc = BalanceRpc()
    def verify():
        return _withdrawal_result(
            entry, "a" * 64, "0x" + "b" * 64, receipt,
            rpc=rpc, rpc_url="https://rpc.test",
        )
    result = verify()
    assert result["status"] == "withdrawn"
    assert result["received_assets"] == [
        {"token_id": TOKEN, "raw": "5"}, {"token_id": other, "raw": "7"},
    ]
    receipt["logs"] = [transfer(TOKEN, 5), transfer(other, 1)]
    result = verify()
    assert result["status"] == "manual_review"
    assert result["reason"] == "output_balance_receipt_mismatch"
    assert json.loads(result["verification_detail"])["receipt_flow_raw"] == "1"
    receipt["logs"] = [transfer(TOKEN, 5), transfer(other, 7, "0x" + "0" * 64)]
    result = verify()
    assert result["status"] == "manual_review"


def test_rabby_output_token_is_verified_by_receipt_and_balance_change():
    from eth_abi import encode
    from eth_utils import keccak

    token = "0x4200000000000000000000000000000000000006"
    market = "0x" + "4" * 40
    raw_received = 105277904533522
    entry = {
        "wallet": WALLET,
        "output_token_ids": [token],
        "action": {
            "func": "redeem(uint256,address,address)()",
            "contract_id": market,
            "str_params": ["99445032919093", WALLET, WALLET],
        },
    }
    transfer = {
        "address": token,
        "topics": [
            "0x" + keccak(text="Transfer(address,address,uint256)").hex(),
            "0x" + market[2:].rjust(64, "0"),
            "0x" + WALLET[2:].rjust(64, "0"),
        ],
        "data": "0x" + f"{raw_received:064x}",
    }
    receipt = {"status": "0x1", "blockNumber": "0x64", "logs": [transfer]}

    class BalanceRpc:
        def call(self, url, method, params):
            assert method == "eth_call"
            assert params[0]["to"] == token
            if params[0]["data"] == "0x313ce567":
                return "0x" + f"{18:064x}"
            if params[0]["data"] == "0x95d89b41":
                return "0x" + encode(["string"], ["WETH"]).hex()
            return "0x" + f"{(raw_received if params[1] == '0x64' else 0):064x}"

    result = _withdrawal_result(
        entry, "a" * 64, "0x" + "b" * 64, receipt,
        rpc=BalanceRpc(), rpc_url="https://rpc.test",
    )
    assert result["status"] == "withdrawn"
    assert result["received_token_id"] == token
    assert result["received_raw"] == str(raw_received)
    assert result["received_assets"][0]["amount"] == "0.000105277904533522"
    assert result["received_assets"][0]["symbol"] == "WETH"


def test_token_receipt_requires_actual_wallet_balance_gain():
    from eth_utils import keccak

    escrow = "0xd4888870c8686c748232719051b677791dbda26d"
    stg = "0xb0d502e938ed5f4df2e681fe6e419ff29631d62b"
    amount = 2 * 10**18
    wallet_topic = "0x" + WALLET[2:].rjust(64, "0")
    escrow_topic = "0x" + escrow[2:].rjust(64, "0")
    entry = {
        "wallet": WALLET, "chain_id": 56, "protocol_id": "bsc_stargate",
        "pool_id": escrow, "output_token_ids": [stg],
        "action": {"func": "withdraw()()", "contract_id": escrow},
    }
    transfer = {
        "address": stg,
        "topics": ["0x" + keccak(text="Transfer(address,address,uint256)").hex(),
                   escrow_topic, wallet_topic],
        "data": "0x" + f"{amount:064x}",
    }
    class BalanceRpc:
        def __init__(self, after):
            self.after = after

        def call(self, url, method, params):
            assert method == "eth_call"
            return "0x" + f"{(self.after if params[1] == '0x64' else 0):064x}"

    receipt = {"status": "0x1", "blockNumber": "0x64", "logs": [transfer]}
    result = _withdrawal_result(
        entry, "a" * 64, "0x" + "b" * 64, receipt,
        rpc=BalanceRpc(amount), rpc_url="https://rpc.test",
    )
    assert result["status"] == "withdrawn"
    assert result["received_raw"] == str(amount)
    result = _withdrawal_result(
        entry, "a" * 64, "0x" + "b" * 64, receipt,
        rpc=BalanceRpc(0), rpc_url="https://rpc.test",
    )
    assert result["status"] == "manual_review"


def test_native_output_uses_balance_gain_after_transaction_fee():
    fuel = "0x19b5cc75846bf6286d599ec116536a333c4c2c14"
    amount = 1023000000000000
    action = {
        "type": "withdraw", "contract_id": fuel,
        "func": "withdraw(address,address,uint240)()",
        "str_params": ["0x" + "0" * 40, WALLET, str(amount)],
    }
    item = {
        "name": "Staked", "pool": {"id": fuel, "chain": "eth"},
        "position_index": "eth", "proxy_detail": {},
        "stats": {"net_usd_value": 3, "debt_usd_value": 0},
        "detail": {"supply_token_list": [{"id": "eth", "amount": 0.001023}]},
        "withdraw_actions": [action],
    }
    class FuelRabby:
        def chain_ids(self):
            return {"eth": 1}

        def positions(self, wallet):
            return [{"id": "fuel", "chain": "eth", "portfolio_item_list": [item]}]

    class NativeChainRpc(Rpc):
        def call(self, url, method, params):
            if method == "eth_chainId":
                return "0x1"
            return super().call(url, method, params)

    rabby = FuelRabby()
    plan = create_defi_plan([WALLET], rabby, supported_chain_ids={1}, now_seconds=100)
    raw = json.dumps(plan).encode()
    digest = hashlib.sha256(raw).hexdigest()
    action_id = plan["entries"][0]["action_id"]
    prepared = prepare_defi_action(raw, plan_sha256=digest, action_id=action_id,
        wallet=WALLET, rabby=rabby, rpc=NativeChainRpc(), rpc_url="https://rpc.test",
        max_gas_wei=1_000_000, now_seconds=101)
    assert prepared.entry["output_token_ids"] == ["eth"]
    class NativeRpc(NativeChainRpc):
        def __init__(self, received):
            super().__init__()
            self.received = received

        def call(self, url, method, params):
            if method == "eth_getBalance" and params[1] in {"0x63", "0x64"}:
                before = 10**18
                balance = before if params[1] == "0x63" else before + self.received - 100000
                return hex(balance)
            if method == "eth_getTransactionByHash":
                return {"from": WALLET, "value": "0x0"}
            return super().call(url, method, params)

    receipt = {"status": "0x1", "blockNumber": "0x64", "gasUsed": "0x186a0",
               "effectiveGasPrice": "0x1", "logs": []}
    entry = prepared.entry
    good = _withdrawal_result(entry, action_id, "0x" + "b" * 64, receipt,
                              rpc=NativeRpc(amount), rpc_url="https://rpc.test")
    assert good["status"] == "withdrawn"
    assert good["received_raw"] == str(amount)
    bad = _withdrawal_result(entry, action_id, "0x" + "b" * 64, receipt,
                             rpc=NativeRpc(0), rpc_url="https://rpc.test")
    assert bad["status"] == "manual_review"


def test_withdraw_is_journaled_and_a_second_run_only_observes_it(tmp_path, monkeypatch):
    from eth_utils import keccak

    from evm_inventory import defi_execution

    class SendingRpc(Rpc):
        def __init__(self):
            super().__init__()
            self.send_count = 0
            self.token_balance = 0
            self.tx_hash = None

        def call(self, url, method, params):
            self.calls.append(method)
            if method == "eth_call":
                if params[0].get("to") == "0x420000000000000000000000000000000000000f":
                    return "0x" + f"{1:064x}"
                if params[0]["data"].startswith("0x70a08231") and params[1] == "0x63":
                    return "0x" + "0" * 64
                return (
                    "0x" + f"{self.token_balance:064x}"
                    if params[0]["data"].startswith("0x70a08231")
                    else "0x"
                )
            if method == "eth_getTransactionCount":
                return "0x0"
            if method == "eth_sendRawTransaction":
                self.send_count += 1
                self.tx_hash = "0x" + keccak(bytes.fromhex(params[0][2:])).hex()
                self.token_balance = 10
                return self.tx_hash
            if method == "eth_getTransactionReceipt":
                topic = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
                return {"status": "0x1", "blockNumber": "0x64", "logs": [{
                    "address": TOKEN,
                    "topics": [topic, "0x" + ROUTER[2:].rjust(64, "0"),
                               "0x" + WALLET[2:].rjust(64, "0")],
                    "data": "0x" + f"{10:064x}",
                }]}
            if method == "eth_getTransactionByHash":
                return {"hash": self.tx_hash}
            return super().call(url, method, params)

    monkeypatch.setattr(defi_execution, "sign_transaction", lambda *a, **kw: "0xdeadbeef")
    raw, digest, action_id = sample_plan()
    rpc = SendingRpc()
    row = WalletWorkbookRow(2, 1, WALLET, "0x" + "a" * 64, "")
    db = tmp_path / "inventory.sqlite"
    with Store(db) as store:
        store.save_defi_plan(digest, raw)
    kwargs = {
        "plan_sha256": digest, "action_id": action_id, "wallet_row": row,
        "rabby": Rabby(), "rpc": rpc, "rpc_url": "https://rpc.test",
        "journal_path": db, "max_gas_wei": 1_000_000,
        "execute": True,
    }
    first = execute_defi_action(raw, **kwargs, now_seconds=101)
    assert first["status"] == "withdrawn"
    assert first["received_raw"] == "10"
    assert rpc.send_count == 1
    with Journal(db) as journal:
        position = journal.get_or_create_position(
            position_key=f"defi:{WALLET}:{action_id}", wallet=WALLET
        )
        step = journal.latest_step(position_id=position["id"], step_key="defi_withdraw")
        assert step["state"] == "confirmed"
    second = execute_defi_action(raw, **kwargs, now_seconds=1001)
    assert second["status"] == "withdrawn"
    assert second["received_raw"] == first["received_raw"]
    assert second["tx_hash"] == first["tx_hash"]
    assert rpc.send_count == 1
    with Store(db, readonly=True) as store:
        assert store.defi_plan(digest) == raw
    assert list(tmp_path.glob("*.sqlite")) == [db]


def test_action_lock_refuses_concurrent_execution(tmp_path):
    path = tmp_path / "defi.sqlite"
    with _action_lock(path):
        with pytest.raises(ValueError, match="another writer"):
            with _action_lock(path):
                pass


def test_unresolved_prior_intent_blocks_another_nonce(tmp_path, monkeypatch):
    from evm_inventory import defi_execution

    raw, digest, action_id = sample_plan()
    path = tmp_path / "defi.sqlite"
    with Journal(path) as journal:
        position = journal.get_or_create_position(
            position_key=f"defi:{WALLET}:{action_id}", wallet=WALLET
        )
        journal.record_step_intent(
            position_id=position["id"], step_key="defi_withdraw", nonce=0,
            calldata_digest="a" * 64, signed_payload_digest="b" * 64,
        )
    monkeypatch.setattr(defi_execution, "sign_transaction", lambda *a, **kw: "0xdeadbeef")
    rpc = Rpc()
    with pytest.raises(ValueError, match="unresolved prior DeFi intent"):
        execute_defi_action(
            raw, plan_sha256=digest, action_id=action_id,
            wallet_row=WalletWorkbookRow(2, 1, WALLET, "0x" + "a" * 64, ""),
            rabby=Rabby(), rpc=rpc, rpc_url="https://rpc.test",
            journal_path=path, max_gas_wei=1_000_000,
            execute=True, now_seconds=101,
        )
    assert "eth_sendRawTransaction" not in rpc.calls
