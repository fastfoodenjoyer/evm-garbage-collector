"""Upgrade regressions for the two separate legacy source-result/cost commits."""

from decimal import Decimal

import pytest

from evm_inventory.journal import Journal

WALLET = "0x" + "1" * 40
DEPOSIT = "0x" + "2" * 40
SOURCE_LOSS = "2"
SIBLING_LOSS = "3"
SOURCE_RESULT = dict(
    state="confirmed",
    actual_asset_id="native",
    actual_balance_raw="100",
    realized_loss_usd=SOURCE_LOSS,
)


def legacy_state(journal, *, window, sibling):
    group = journal.get_or_create_group(
        group_key="legacy",
        wallet=WALLET,
        source_chain_id=10,
        source_usd="100",
        loss_budget_pct="15",
    )
    journal.record_group_state(group["id"], "active")
    position = journal.get_or_create_position(
        position_key="legacy:source",
        wallet=WALLET,
        group_id=group["id"],
        source_asset_id="0x" + "a" * 40,
        source_amount_raw="100",
    )
    journal.record_position_state(position["id"], "swap_submitted")
    journal.record_step_intent(
        position_id=position["id"],
        step_key="swap_to_native:source:0",
        nonce=0,
        calldata_digest="a" * 64,
        signed_payload_digest="b" * 64,
        tx_hash="0x" + "a" * 64,
    )
    # This is the acc747f first commit, without the new atomic accounting write.
    journal.save_checkpoint(position["id"], "swap_result", SOURCE_RESULT)
    sibling_position = None
    base = Decimal(0)
    if sibling != "none":
        sibling_position = journal.get_or_create_position(
            position_key="legacy:sibling",
            wallet=WALLET,
            group_id=group["id"],
            source_asset_id="0x" + "b" * 40,
            source_amount_raw="200",
        )
        if sibling == "accounted":
            journal.record_position_state(sibling_position["id"], "direct_deposit_submitted")
            journal.account_position_loss(sibling_position["id"], loss_usd=SIBLING_LOSS)
            journal.record_position_state(sibling_position["id"], "completed")
        else:
            journal.record_position_state(sibling_position["id"], "bridge_submitted")
            source_hash = "0x" + "b" * 64
            journal.record_step_intent(
                position_id=sibling_position["id"],
                step_key="bridge:sibling:0",
                nonce=1,
                calldata_digest="c" * 64,
                signed_payload_digest="d" * 64,
                tx_hash=source_hash,
            )
            if sibling == "legacy_completed":
                target = dict(
                    coin="USDC",
                    chain="BASE",
                    chain_id=8453,
                    asset_id="0x" + "c" * 40,
                    decimals=6,
                    minimum_raw="1",
                )
                journal.save_checkpoint(
                    sibling_position["id"],
                    "prepared",
                    dict(wallet=WALLET, target=target, deposit_address=DEPOSIT),
                )
                journal.save_checkpoint(
                    sibling_position["id"],
                    "route_deposit",
                    dict(
                        target=target,
                        recipient=DEPOSIT,
                        tx_hash=source_hash,
                        source_hash=source_hash,
                        step_key="bridge:sibling:0",
                        started_ms=1000,
                        realized_loss_usd=SIBLING_LOSS,
                    ),
                )
                journal.save_checkpoint(
                    sibling_position["id"],
                    "bitget_credit",
                    dict(
                        status="success",
                        tx_hash=source_hash,
                        recipient=DEPOSIT,
                        coin="USDC",
                        chain="BASE",
                    ),
                )
                journal.record_position_state(sibling_position["id"], "completed")
        base = Decimal(SIBLING_LOSS)
    # The authoritative legacy aggregate may include a completed sibling with
    # either explicit position accounting or its old group-only accounting.
    charged = base + (Decimal(SOURCE_LOSS) if window != "before_group_write" else 0)
    journal.record_group_realized_loss(group["id"], realized_loss_usd=str(charged))
    if window == "after_position_state":
        # In acc747f this state/output write always followed the group fee write.
        journal.record_position_state(
            position["id"],
            "source_asset_converted_bridge_pending",
            actual_asset_id="native",
            actual_balance_raw=100,
        )
    return group["id"], position["id"], sibling_position


@pytest.mark.parametrize(
    "window", ["before_group_write", "after_group_write_before_position", "after_position_state"]
)
@pytest.mark.parametrize("sibling", ["none", "accounted", "legacy_completed"])
def test_legacy_source_fee_adoption_never_loses_or_counts_the_fee_twice(tmp_path, window, sibling):
    path = tmp_path / "fake.sqlite"
    with Journal(path) as journal:
        group_id, position_id, sibling_position = legacy_state(
            journal, window=window, sibling=sibling
        )
        assert journal.position(position_id)["accounted_loss_usd"] is None
        expected = Decimal(SOURCE_LOSS) + (Decimal(SIBLING_LOSS) if sibling != "none" else 0)
        assert Decimal(journal.checkpoint_source_conversion(position_id, SOURCE_RESULT)) == expected
        assert journal.position(position_id)["accounted_loss_usd"] == SOURCE_LOSS
        assert Decimal(journal.group(group_id)["realized_loss_usd"]) == expected
        if sibling_position:
            expected_sibling = SIBLING_LOSS if sibling == "accounted" else None
            assert (
                journal.position(sibling_position["id"])["accounted_loss_usd"] == expected_sibling
            )
    # The adopted marker/state and aggregate must survive reopening and replay.
    with Journal(path) as journal:
        assert Decimal(journal.checkpoint_source_conversion(position_id, SOURCE_RESULT)) == expected
        assert Decimal(journal.group(group_id)["realized_loss_usd"]) == expected
        assert Decimal(journal.account_position_loss(position_id, loss_usd="5")) == expected + 3
        assert Decimal(journal.account_position_loss(position_id, loss_usd="5")) == expected + 3


@pytest.mark.parametrize("window", ["before_group_write", "after_group_write_before_position"])
def test_ambiguous_legacy_sibling_costs_preserve_the_authoritative_aggregate(tmp_path, window):
    with Journal(tmp_path / "fake.sqlite") as journal:
        group_id, position_id, _ = legacy_state(journal, window=window, sibling="unattributed")
        before_group = journal.group(group_id)
        before_position = journal.position(position_id)
        before_result = journal.checkpoint(position_id, "swap_result")
        with pytest.raises(ValueError, match="legacy_source_cost_accounting_ambiguous"):
            journal.checkpoint_source_conversion(position_id, SOURCE_RESULT)
        assert journal.group(group_id) == before_group
        assert journal.position(position_id) == before_position
        assert journal.checkpoint(position_id, "swap_result") == before_result
