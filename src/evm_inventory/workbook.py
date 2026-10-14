"""XLSX input and dry-run output for wallet operations."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from .models import ConfigError

WORKSHEET_NAME = "Wallets"
WORKSHEET_HEADERS = (
    "# (номер)",
    "Публичный адрес",
    "Приватный ключ",
    "Адрес депозита Bitget",
    "Предлагаемые действия",
    "Прокси Rabby",
)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ORDINAL_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")

DEPOSIT_ADDRESS_WARNING = (
    "ОБЯЗАТЕЛЬНО ПРОВЕРЬТЕ ЧТО ЗА АДРЕСА ДЕПОЗИТОВ ВЫ ТУДА ВСТАВИЛИ. "
    "Публичный API Bitget не предоставляет полный список дополнительных депозитных адресов. "
    "Код доверяет адресам из таблицы и не проверяет их принадлежность Bitget. "
    "За адреса и выбранные сети отвечает тот, кто заполнил таблицу."
)


def require_trusted_deposit_addresses(trusted: bool) -> None:
    """Require explicit operator acknowledgement before executing deposit routes."""
    if not trusted:
        raise ConfigError(
            DEPOSIT_ADDRESS_WARNING
            + " Запуск заблокирован. После проверки адресов добавьте --trust-deposit-addresses."
        )


def require_unique_deposit_addresses(
    addresses: Iterable[tuple[int, object]], *, allow_duplicates: bool = False
) -> None:
    """Check the full table, including rows outside the selected wallet range."""
    if allow_duplicates:
        return
    rows_by_address: dict[str, list[int]] = {}
    for row_number, value in addresses:
        if isinstance(value, str) and value.strip():
            rows_by_address.setdefault(value.strip().lower(), []).append(row_number)
    duplicates = [
        f"deposit address {address}: Excel rows {', '.join(map(str, rows))}"
        for address, rows in rows_by_address.items()
        if len(rows) > 1
    ]
    if duplicates:
        raise ConfigError(
            "duplicate Bitget deposit addresses: "
            + "; ".join(duplicates)
            + ". To explicitly allow duplicates, use --allow-duplicate-deposit-addresses."
        )


@dataclass(frozen=True, slots=True)
class WalletWorkbookRow:
    row_number: int
    ordinal: int
    public_address: str
    private_key: str = field(repr=False)
    bitget_deposit_address: str
    rabby_proxy: str = field(default="", repr=False)
    proxy_pool: tuple[str, ...] = field(default=(), repr=False)


def _rabby_proxy(value: object, *, row_number: int, required: bool) -> str:
    if value is None or value == "":
        if required:
            raise ConfigError(f"row {row_number}: Rabby proxy is required")
        return ""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or any(character in value for character in "\r\n\t")
    ):
        raise ConfigError(f"row {row_number}: invalid Rabby proxy")
    proxy = value
    try:
        parsed = urlsplit(proxy)
        valid = (
            parsed.scheme in {"http", "https", "socks5"}
            and bool(parsed.hostname)
            and parsed.port is not None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f"row {row_number}: invalid Rabby proxy")
    return proxy


def parse_ordinal_ranges(spec: str | None) -> tuple[tuple[int, int], ...] | None:
    """Parse inclusive ordinal ranges such as ``1-50,75,100-120``."""

    if spec is None:
        return None
    parts = [part.strip() for part in spec.split(",")]
    if not parts or any(not part for part in parts):
        raise ConfigError("wallet ranges must be comma-separated positive numbers or ranges")
    ranges: list[tuple[int, int]] = []
    for part in parts:
        match = _ORDINAL_RANGE_RE.fullmatch(part)
        if match is None:
            raise ConfigError(f"invalid wallet range {part!r}")
        start = int(match.group(1))
        end = int(match.group(2) or match.group(1))
        if start <= 0 or end <= 0 or end < start:
            raise ConfigError(f"invalid wallet range {part!r}")
        ranges.append((start, end))
    for index, current in enumerate(ranges):
        for previous in ranges[:index]:
            if current[0] <= previous[1] and previous[0] <= current[1]:
                raise ConfigError("wallet ranges must not overlap")
    return tuple(ranges)


def wallet_range_batches(
    rows: tuple[WalletWorkbookRow, ...],
    ranges: tuple[tuple[int, int], ...] | None,
) -> tuple[tuple[WalletWorkbookRow, ...], ...]:
    """Return selected rows grouped by requested ranges, preserving sheet order."""

    if ranges is None:
        return (rows,)
    batches: list[tuple[WalletWorkbookRow, ...]] = []
    found: set[int] = set()
    for start, end in ranges:
        batch = tuple(row for row in rows if start <= row.ordinal <= end)
        found.update(row.ordinal for row in batch)
        batches.append(batch)
    requested = {ordinal for start, end in ranges for ordinal in range(start, end + 1)}
    missing = sorted(requested - found)
    if missing:
        preview = ", ".join(str(item) for item in missing[:10])
        suffix = "..." if len(missing) > 10 else ""
        raise ConfigError(f"wallet ordinal(s) not found in workbook: {preview}{suffix}")
    return tuple(batches)


def create_wallet_template(path: Path) -> None:
    """Create an empty wallet workbook with the required input columns."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = WORKSHEET_NAME
    for index, header in enumerate(WORKSHEET_HEADERS, start=1):
        cell = sheet.cell(row=1, column=index, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    sheet.freeze_panes = "A2"
    for column, width in zip(
        ("A", "B", "C", "D", "E", "F"), (8, 46, 72, 46, 72, 52), strict=True
    ):
        sheet.column_dimensions[column].width = width
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _required_string(value: object, *, row_number: int, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"row {row_number}: {field_name} is required")
    return value.strip()


def _proxy_only(values: tuple[object, ...]) -> bool:
    return len(values) > 5 and bool(values[5]) and all(
        value is None or value == "" for value in values[:5]
    )


def load_proxy_assignments(path: Path) -> dict[str, tuple[str, ...]]:
    """Assign reserves in physical wallet order, independently of selected ranges.

    Private keys are neither parsed nor validated. Other input cells are only
    inspected for occupancy to distinguish wallet rows from reserve rows.
    """
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except OSError as exc:
        raise ConfigError("cannot read proxy assignment workbook") from exc
    try:
        if WORKSHEET_NAME not in workbook.sheetnames:
            raise ConfigError(f"workbook must contain a {WORKSHEET_NAME!r} sheet")
        sheet = workbook[WORKSHEET_NAME]
        if tuple(cell.value for cell in sheet[1]) != WORKSHEET_HEADERS:
            raise ConfigError("workbook headers do not match the wallet template")
        wallets: list[tuple[int, str, str]] = []
        reserves: list[tuple[int, str]] = []
        ordinals: set[int] = set()
        addresses: set[str] = set()
        proxy_rows: dict[str, int] = {}
        for row_number, values in enumerate(
            sheet.iter_rows(min_row=2, values_only=True), start=2
        ):
            if all(value is None or value == "" for value in values):
                continue
            proxy = _rabby_proxy(values[5], row_number=row_number, required=True)
            if proxy in proxy_rows:
                raise ConfigError(
                    f"row {row_number}: duplicate Rabby proxy (row {proxy_rows[proxy]})"
                )
            proxy_rows[proxy] = row_number
            if _proxy_only(values):
                if not wallets:
                    raise ConfigError(f"row {row_number}: proxy-only row must follow all wallets")
                reserves.append((row_number, proxy))
                continue
            if reserves:
                raise ConfigError(
                    f"row {reserves[0][0]}: proxy-only row must follow all wallets; "
                    f"row {row_number}: unexpected wallet row"
                )
            ordinal = values[0]
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
                raise ConfigError(f"row {row_number}: # must be a positive integer")
            if ordinal in ordinals:
                raise ConfigError(f"row {row_number}: duplicate #")
            address = _required_string(
                values[1], row_number=row_number, field_name="public address"
            ).lower()
            if not _ADDRESS_RE.fullmatch(address):
                raise ConfigError(f"row {row_number}: invalid public EVM address")
            if address in addresses:
                raise ConfigError(f"row {row_number}: duplicate public address")
            ordinals.add(ordinal)
            addresses.add(address)
            wallets.append((row_number, address, proxy))
        if not wallets:
            raise ConfigError("workbook must contain at least one wallet")
        if reserves and len(reserves) < len(wallets):
            raise ConfigError(f"row {reserves[0][0]}: incomplete first reserve set")
        pools = {address: [proxy] for _, address, proxy in wallets}
        for index, (_, proxy) in enumerate(reserves):
            pools[wallets[index % len(wallets)][1]].append(proxy)
        return {address: tuple(pool) for address, pool in pools.items()}
    finally:
        workbook.close()


def load_wallet_workbook(
    path: Path,
    *,
    require_deposit_address: bool = True,
    require_rabby_proxy: bool = False,
    ordinal_ranges: tuple[tuple[int, int], ...] | None = None,
    allow_duplicate_deposit_addresses: bool = False,
    check_deposit_uniqueness: bool | None = None,
) -> tuple[WalletWorkbookRow, ...]:
    """Read and validate operator input without persisting private keys."""

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except OSError as exc:
        raise ConfigError(f"cannot read workbook {path}: {exc}") from exc
    if WORKSHEET_NAME not in workbook.sheetnames:
        raise ConfigError(f"workbook must contain a {WORKSHEET_NAME!r} sheet")
    sheet = workbook[WORKSHEET_NAME]
    headers = tuple(cell.value for cell in sheet[1])
    if headers != WORKSHEET_HEADERS and not (
        not require_rabby_proxy and headers == WORKSHEET_HEADERS[:-1]
    ):
        raise ConfigError("workbook headers do not match the wallet template")
    has_proxy_column = headers == WORKSHEET_HEADERS
    has_reserves = has_proxy_column and any(
        _proxy_only(values)
        for values in sheet.iter_rows(min_row=2, values_only=True)
    )
    proxy_assignments = (
        load_proxy_assignments(path) if has_reserves or require_rabby_proxy else {}
    )
    if check_deposit_uniqueness is True or (
        check_deposit_uniqueness is None and require_deposit_address
    ):
        require_unique_deposit_addresses(
            (
                (number, value[0])
                for number, value in enumerate(
                    sheet.iter_rows(min_row=2, min_col=4, max_col=4, values_only=True), start=2
                )
            ),
            allow_duplicates=allow_duplicate_deposit_addresses,
        )

    rows: list[WalletWorkbookRow] = []
    seen_ordinals: set[int] = set()
    seen_wallets: set[str] = set()
    issues: list[str] = []
    for row_number, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        if all(value is None or value == "" for value in values):
            continue
        if has_reserves and _proxy_only(values):
            continue
        try:
            ordinal = values[0]
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
                raise ConfigError(f"row {row_number}: # must be a positive integer")
            if ordinal_ranges is not None and not any(
                start <= ordinal <= end for start, end in ordinal_ranges
            ):
                continue
            if ordinal in seen_ordinals:
                raise ConfigError(f"row {row_number}: duplicate # {ordinal}")
            public_address = _required_string(
                values[1], row_number=row_number, field_name="public address"
            ).lower()
            private_key = _required_string(
                values[2], row_number=row_number, field_name="private key"
            )
            deposit_address = (
                _required_string(
                    values[3], row_number=row_number, field_name="Bitget deposit address"
                ).lower()
                if require_deposit_address or values[3]
                else ""
            )
            rabby_proxy = _rabby_proxy(
                values[5] if has_proxy_column and len(values) > 5 else None,
                row_number=row_number,
                required=require_rabby_proxy,
            )
            if not _ADDRESS_RE.fullmatch(public_address):
                raise ConfigError(f"row {row_number}: invalid public EVM address")
            if not _PRIVATE_KEY_RE.fullmatch(private_key):
                raise ConfigError(
                    f"row {row_number}: private key must be 0x plus 64 hex characters"
                )
            if deposit_address and not _ADDRESS_RE.fullmatch(deposit_address):
                raise ConfigError(f"row {row_number}: invalid Bitget EVM deposit address")
            if public_address in seen_wallets:
                raise ConfigError(f"row {row_number}: duplicate public address")
            seen_ordinals.add(ordinal)
            seen_wallets.add(public_address)
            rows.append(
                WalletWorkbookRow(
                    row_number=row_number,
                    ordinal=ordinal,
                    public_address=public_address,
                    private_key=private_key,
                    bitget_deposit_address=deposit_address,
                    rabby_proxy=rabby_proxy,
                    proxy_pool=proxy_assignments.get(public_address, ()),
                )
            )
        except ConfigError as exc:
            issues.extend(exc.issues)
    if issues:
        raise ConfigError("invalid wallet workbook: " + "; ".join(issues), issues=tuple(issues))
    if not rows:
        raise ConfigError("workbook must contain at least one wallet")
    return tuple(rows)


def dry_run_workbook(
    path: Path, *, allow_duplicate_deposit_addresses: bool = False
) -> dict[str, int]:
    """Validate all inputs, then atomically write placeholder plans to the workbook."""

    rows = load_wallet_workbook(
        path, allow_duplicate_deposit_addresses=allow_duplicate_deposit_addresses
    )
    workbook = load_workbook(path)
    sheet = workbook[WORKSHEET_NAME]
    for row in rows:
        sheet.cell(row=row.row_number, column=5, value="Маршруты еще не рассчитаны")
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".xlsx", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        workbook.save(temporary_path)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {"wallets": len(rows), "actions_written": len(rows)}
