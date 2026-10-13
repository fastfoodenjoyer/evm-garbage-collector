"""Read-only proofs for adopting costs written by the legacy swap executor."""

from decimal import Decimal, InvalidOperation, localcontext


class LegacySourceCostAmbiguous(ValueError):
    def __init__(self):
        super().__init__("legacy_source_cost_accounting_ambiguous")


def money(value):
    if value is None or isinstance(value, (bool, float)):
        raise LegacySourceCostAmbiguous()
    try:
        result = Decimal(value)
        if not result.is_finite() or result < 0:
            raise LegacySourceCostAmbiguous()
        return result
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LegacySourceCostAmbiguous() from exc


def terminal_remaining_cost(journal, position):
    """A terminal, correlated old route cost was written before its completion state."""
    context = journal.checkpoint(position["id"], "route_deposit") or {}
    target = context.get("target")
    if (
        position["state"] != "completed"
        or not isinstance(target, dict)
        or not target.get("asset_id")
        or not isinstance(target.get("chain_id"), int)
        or not journal._completed_route_credit_verified(position["id"])
    ):
        raise LegacySourceCostAmbiguous()
    prepared = journal.checkpoint(position["id"], "bridge_prepared") or journal.checkpoint(
        position["id"], "prepared"
    )
    # Bind the credited cost to the original position's prepared target/recipient.
    if prepared is None or (
        prepared.get("target") != target
        or prepared.get("deposit_address") != context.get("recipient")
        or str(prepared.get("wallet", "")).lower() != position["wallet"]
    ):
        raise LegacySourceCostAmbiguous()
    return money(context.get("realized_loss_usd"))


def sibling_cost(journal, position):
    if position["accounted_loss_usd"] is not None:
        amount = money(position["accounted_loss_usd"])
        # Before v2 a swapped position's accounted value excludes the group-only source fee.
        source = journal.checkpoint(position["id"], "swap_result")
        if (
            source is not None
            and journal.checkpoint(position["id"], "source_cost_provenance") is None
        ):
            if position["state"] != "completed":
                raise LegacySourceCostAmbiguous()
            amount += money(source.get("realized_loss_usd"))
        return amount
    steps = journal.connection.execute(
        "SELECT 1 FROM route_steps WHERE position_id=?", (position["id"],)
    ).fetchone()
    if (
        position["state"] == "planned"
        and steps is None
        and journal.checkpoint(position["id"], "swap_result") is None
        and journal.checkpoint(position["id"], "route_deposit") is None
    ):
        # No durable execution and a still planned member establish an explicit zero cost.
        return Decimal(0)
    amount = terminal_remaining_cost(journal, position)
    source = journal.checkpoint(position["id"], "swap_result")
    return amount + (money(source.get("realized_loss_usd")) if source is not None else Decimal(0))


def legacy_baseline(journal, position_id, result):
    """Find an exact ledger decomposition; never interpret NULL accounting as zero paid."""
    position = journal.position(position_id)
    source = money(result.get("realized_loss_usd"))
    group = journal.group(position["group_id"])
    paid = money(group["realized_loss_usd"])
    rows = [
        dict(row)
        for row in journal.connection.execute(
            "SELECT * FROM route_positions WHERE group_id=?", (group["id"],)
        )
    ]
    if any(row["wallet"] != position["wallet"] for row in rows):
        raise LegacySourceCostAmbiguous()
    with localcontext() as context:
        context.prec = 80
        others = sum(
            (sibling_cost(journal, row) for row in rows if row["id"] != position_id), Decimal(0)
        )
        own_remaining = (
            money(position["accounted_loss_usd"])
            if position["accounted_loss_usd"] is not None
            else None
        )
        deposit = journal.checkpoint(position_id, "route_deposit")
        if own_remaining is not None:
            remaining_options = [own_remaining]
        elif deposit is not None:
            remaining_options = list({Decimal(0), money(deposit.get("realized_loss_usd"))})
        else:
            # This row is the source phase; no paid dependent cost has been recorded.
            if journal.connection.execute(
                "SELECT 1 FROM route_steps WHERE position_id=? "
                "AND step_key NOT LIKE 'swap_to_native:%'",
                (position_id,),
            ).fetchone():
                raise LegacySourceCostAmbiguous()
            remaining_options = [Decimal(0)]
        charged_proof = (
            position["state"] == "source_asset_converted_bridge_pending"
            and position["actual_asset_id"] == result["actual_asset_id"]
            and position["actual_balance_raw"] == result["actual_balance_raw"]
        )
        for event in journal.position_events(position_id):
            if (
                event["event_type"] == "requote_after_swap"
                and event["actual_asset_id"] == result["actual_asset_id"]
            ):
                charged_proof = True
        combinations = {
            (charged, remaining)
            for charged in (False, True)
            for remaining in remaining_options
            if others + remaining + (source if charged else 0) == paid
            and (not charged_proof or charged)
        }
        if len(combinations) != 1:
            raise LegacySourceCostAmbiguous()
        charged, remaining = combinations.pop()
        return dict(
            version=2,
            origin="legacy_adoption",
            source_loss_usd=format(source, "f"),
            cumulative_loss_usd=format(source + remaining, "f"),
            group_loss_before_usd=format(paid, "f"),
            group_loss_after_usd=format(paid + (0 if charged else source), "f"),
            legacy_remaining_loss_usd=(deposit or {}).get("realized_loss_usd"),
        )
