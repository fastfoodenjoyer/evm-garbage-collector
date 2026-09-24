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


def test_liquidity_receipt_proves_both_minimum_outputs():
    from evm_inventory.defi_execution import _TRANSFER_TOPIC

    other = "0x" + "4" * 40
    action = {
        "func": "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)",
        "str_params": [TOKEN, other, "100", "5", "7", WALLET, "9999"],
    }
    entry = {"wallet": WALLET, "action": action}
    recipient = "0x" + WALLET[2:].rjust(64, "0")

    def transfer(token, amount, to=recipient):
        return {
            "address": token,
            "topics": [_TRANSFER_TOPIC, "0x" + "0" * 64, to],
            "data": "0x" + f"{amount:064x}",
        }

    receipt = {"status": "0x1", "logs": [transfer(TOKEN, 5), transfer(other, 7)]}
    assert _withdrawal_result(entry, "a" * 64, "0x" + "b" * 64, receipt)["status"] == "withdrawn"
    receipt["logs"] = [transfer(TOKEN, 5), transfer(other, 1)]
    result = _withdrawal_result(entry, "a" * 64, "0x" + "b" * 64, receipt)
    assert result["status"] == "manual_review"
    receipt["logs"] = [transfer(TOKEN, 5), transfer(other, 7, "0x" + "0" * 64)]
    result = _withdrawal_result(entry, "a" * 64, "0x" + "b" * 64, receipt)
    assert result["status"] == "manual_review"


def test_stargate_locked_withdrawal_is_verified_from_its_receipt():
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
    withdraw = {
        "address": escrow,
        "topics": ["0x" + keccak(text="Withdraw(address,uint256,uint256)").hex(),
                   wallet_topic],
        "data": "0x" + amount.to_bytes(32, "big").hex() + (1).to_bytes(32, "big").hex(),
    }
    receipt = {"status": "0x1", "logs": [transfer, withdraw]}
    result = _withdrawal_result(entry, "a" * 64, "0x" + "b" * 64, receipt)
    assert result["status"] == "withdrawn"
    assert result["received_raw"] == str(amount)
    receipt["logs"] = [transfer]
    result = _withdrawal_result(entry, "a" * 64, "0x" + "b" * 64, receipt)
    assert result["status"] == "manual_review"


def test_fuel_native_withdraw_requires_exact_event_and_deposit_balance():
    from eth_utils import keccak

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

    class FuelRpc(Rpc):
        def __init__(self, balance):
            super().__init__()
            self.balance = balance

        def call(self, url, method, params):
            if method == "eth_chainId":
                return "0x1"
            if method == "eth_call" and params[0]["data"].startswith("0xd4fac45d"):
                return "0x" + f"{self.balance:064x}"
            return super().call(url, method, params)

    rabby = FuelRabby()
    plan = create_defi_plan([WALLET], rabby, supported_chain_ids={1}, now_seconds=100)
    raw = json.dumps(plan).encode()
    digest = hashlib.sha256(raw).hexdigest()
    action_id = plan["entries"][0]["action_id"]
    prepared = prepare_defi_action(raw, plan_sha256=digest, action_id=action_id,
        wallet=WALLET, rabby=rabby, rpc=FuelRpc(amount), rpc_url="https://rpc.test",
        max_gas_wei=1_000_000, now_seconds=101)
    assert prepared.entry["output_token_ids"] == ["eth"]
    with pytest.raises(ValueError, match="deposit balance"):
        prepare_defi_action(raw, plan_sha256=digest, action_id=action_id,
            wallet=WALLET, rabby=rabby, rpc=FuelRpc(amount - 1), rpc_url="https://rpc.test",
            max_gas_wei=1_000_000, now_seconds=101)
    topic = "0x" + keccak(text="Withdraw(address,address,address,uint240,uint16)").hex()
    wallet_topic = "0x" + WALLET[2:].rjust(64, "0")
    log = {"address": fuel, "topics": [topic, wallet_topic, wallet_topic, "0x" + "0" * 64],
           "data": "0x" + f"{amount:064x}" + "0" * 64}
    entry = prepared.entry
    good = _withdrawal_result(entry, action_id, "0x" + "b" * 64, {"logs": [log]})
    assert good["status"] == "withdrawn"
    assert good["received_raw"] == str(amount)
    bad = _withdrawal_result(entry, action_id, "0x" + "b" * 64,
        {"logs": [{**log, "data": "0x" + f"{amount - 1:064x}" + "0" * 64}]})
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
                return {"status": "0x1"}
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
    assert first["status"] == "manual_review"
    assert first["reason"] == "output_not_verifiable_from_action"
    assert rpc.send_count == 1
    with Journal(db) as journal:
        position = journal.get_or_create_position(
            position_key=f"defi:{WALLET}:{action_id}", wallet=WALLET
        )
        step = journal.latest_step(position_id=position["id"], step_key="defi_withdraw")
        assert step["state"] == "confirmed"
    second = execute_defi_action(raw, **kwargs, now_seconds=1001)
    assert second["status"] == "manual_review"
    assert second["reason"] == first["reason"]
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
