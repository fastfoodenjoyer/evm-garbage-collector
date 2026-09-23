from pathlib import Path

import pytest
from openpyxl import load_workbook

from evm_inventory.models import ConfigError
from evm_inventory.workbook import (
    WORKSHEET_HEADERS,
    WORKSHEET_NAME,
    WalletWorkbookRow,
    create_wallet_template,
    dry_run_workbook,
    load_wallet_workbook,
    parse_ordinal_ranges,
    wallet_range_batches,
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


def test_rabby_proxy_is_required_per_wallet_and_never_shown_in_repr(tmp_path: Path):
    output = tmp_path / "wallets.xlsx"
    create_wallet_template(output)
    workbook = load_workbook(output)
    sheet = workbook[WORKSHEET_NAME]
    proxy = "http://login:secret@proxy.example:8080"
    sheet.append([1, "0x" + "b" * 40, "0x" + "a" * 64, "", "", proxy])
    sheet.append([2, "0x" + "c" * 40, "0x" + "d" * 64, "", "", ""])
    workbook.save(output)

    rows = load_wallet_workbook(output, require_deposit_address=False)
    assert rows[0].rabby_proxy == proxy
    assert proxy not in repr(rows[0])
    with pytest.raises(ConfigError, match="row 3: Rabby proxy is required"):
        load_wallet_workbook(output, require_deposit_address=False, require_rabby_proxy=True)


def test_rabby_proxy_rejects_invalid_url_without_echoing_credentials(tmp_path: Path):
    output = tmp_path / "wallets.xlsx"
    create_wallet_template(output)
    workbook = load_workbook(output)
    workbook[WORKSHEET_NAME].append(
        [1, "0x" + "b" * 40, "0x" + "a" * 64, "", "", "http://login:secret@proxy.example"]
    )
    workbook.save(output)

    with pytest.raises(ConfigError, match="row 2: invalid Rabby proxy") as error:
        load_wallet_workbook(output, require_deposit_address=False, require_rabby_proxy=True)
    assert "secret" not in str(error.value)


def test_rabby_proxy_accepts_socks5_url_and_rejects_scheme_less_value(tmp_path: Path):
    output = tmp_path / "wallets.xlsx"
    create_wallet_template(output)
    workbook = load_workbook(output)
    sheet = workbook[WORKSHEET_NAME]
    sheet.append([1, "0x" + "b" * 40, "0x" + "a" * 64, "", "", "socks5://user:pass@proxy.example:1080"])
    sheet.append([2, "0x" + "c" * 40, "0x" + "d" * 64, "", "", "proxy.example:8080:user:pass"])
    workbook.save(output)

    with pytest.raises(ConfigError, match="row 3: invalid Rabby proxy"):
        load_wallet_workbook(output, require_deposit_address=False, require_rabby_proxy=True)
    book = load_workbook(output)
    book[WORKSHEET_NAME].delete_rows(3)
    book.save(output)
    rows = load_wallet_workbook(output, require_deposit_address=False, require_rabby_proxy=True)
    assert rows[0].rabby_proxy.startswith("socks5://")


def test_parse_ranges_and_group_rows():
    assert parse_ordinal_ranges("1-2, 5, 8-9") == ((1, 2), (5, 5), (8, 9))
    rows = tuple(
        WalletWorkbookRow(index + 2, ordinal, f"0x{ordinal:040x}", "0x" + "a" * 64, "")
        for index, ordinal in enumerate((1, 2, 5, 8, 9))
    )
    batches = wallet_range_batches(rows, parse_ordinal_ranges("1-2,8-9"))
    assert [[row.ordinal for row in batch] for batch in batches] == [[1, 2], [8, 9]]


def test_selected_workbook_range_ignores_invalid_unselected_rows(tmp_path: Path):
    output = tmp_path / "wallets.xlsx"
    create_wallet_template(output)
    workbook = load_workbook(output)
    workbook[WORKSHEET_NAME].append([1, "0x" + "b" * 40, "0x" + "a" * 64, "0x" + "c" * 40])
    workbook[WORKSHEET_NAME].append([2, "not-an-address", "bad-key", ""])
    workbook.save(output)

    rows = load_wallet_workbook(
        output,
        ordinal_ranges=parse_ordinal_ranges("1"),
    )

    assert [row.ordinal for row in rows] == [1]
