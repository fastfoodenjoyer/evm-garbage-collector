from evm_inventory.route_batch import quote_required_positions
from evm_inventory.route_plan import DepositTarget, Position, classify_position


def test_unsupported_native_dust_never_reaches_quote_stage():
    result = classify_position(
        Position(chain_id=81457, asset_id="native", raw_balance=7_637_728_395_014),
        targets=(),
        quote_floor_raw=100_000_000_000_000,
    )

    assert result.status == "dust"
    assert result.quote_required is False


def test_supported_erc20_above_exchange_minimum_is_direct_deposit():
    result = classify_position(
        Position(
            chain_id=8453,
            asset_id="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            raw_balance=10_000,
        ),
        targets=(
            DepositTarget(
                chain_id=8453,
                asset_id="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                minimum_raw=9_997,
            ),
        ),
        quote_floor_raw=1,
    )

    assert result.status == "direct_deposit"
    assert result.quote_required is False


def test_batch_calls_quote_factory_only_for_quote_required_positions():
    positions = (
        Position(chain_id=81457, asset_id="native", raw_balance=1),
        Position(chain_id=130, asset_id="0x1", raw_balance=200),
        Position(chain_id=8453, asset_id="0x2", raw_balance=200),
    )
    targets = (DepositTarget(chain_id=8453, asset_id="0x2", minimum_raw=100),)
    requested = []

    results = quote_required_positions(
        positions,
        targets=targets,
        quote_floor_raw=100,
        quote=lambda position: requested.append(position) or "quoted",
    )

    assert requested == [positions[1]]
    assert [result.status for result in results] == ["dust", "quoted", "direct_deposit"]
