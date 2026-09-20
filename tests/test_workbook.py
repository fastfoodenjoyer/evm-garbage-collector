from pathlib import Path

import pytest
from openpyxl import load_workbook

from evm_inventory.models import ConfigError
from evm_inventory.workbook import (
    WORKSHEET_HEADERS,
    WORKSHEET_NAME,
    create_wallet_template,
    dry_run_workbook,
    load_wallet_workbook,
)


def test_create_wallet_template_has_required_columns(tmp_path: Path):
    output = tmp_path / "wallets.xlsx"

    create_wallet_template(output)

    workbook = load_workbook(output)
    sheet = workbook[WORKSHEET_NAME]
    assert tuple(cell.value for cell in sheet[1]) == WORKSHEET_HEADERS


def test_dry_run_writes_actions_without_changing_private_key(tmp_path: Path):
    output = tmp_path / "wallets.xlsx"
    create_wallet_template(output)
    workbook = load_workbook(output)
    sheet = workbook[WORKSHEET_NAME]
    secret = "0x" + "a" * 64
    sheet.append([1, "0x" + "b" * 40, secret, "0x" + "c" * 40])
    workbook.save(output)

    summary = dry_run_workbook(output)

    assert summary == {"wallets": 1, "actions_written": 1}
    updated = load_workbook(output)[WORKSHEET_NAME]
    assert updated.cell(row=2, column=3).value == secret
    assert updated.cell(row=2, column=5).value == "Маршруты еще не рассчитаны"


def test_dry_run_rejects_invalid_deposit_address_without_writing_actions(tmp_path: Path):
    output = tmp_path / "wallets.xlsx"
    create_wallet_template(output)
    workbook = load_workbook(output)
    sheet = workbook[WORKSHEET_NAME]
    sheet.append([1, "0x" + "b" * 40, "0x" + "a" * 64, "not-an-address", "keep"])
    workbook.save(output)

    with pytest.raises(ConfigError, match="invalid Bitget EVM deposit address"):
        dry_run_workbook(output)

    assert load_workbook(output)[WORKSHEET_NAME].cell(row=2, column=5).value == "keep"


def test_read_only_planning_accepts_blank_deposit_address(tmp_path: Path):
    output = tmp_path / "wallets.xlsx"
    create_wallet_template(output)
    workbook = load_workbook(output)
    workbook[WORKSHEET_NAME].append([1, "0x" + "b" * 40, "0x" + "a" * 64, ""])
    workbook.save(output)

    rows = load_wallet_workbook(output, require_deposit_address=False)

    assert rows[0].bitget_deposit_address == ""
