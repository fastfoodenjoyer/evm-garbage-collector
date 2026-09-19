# EVM Inventory

Read-only inventory of native coins and a curated stablecoin list across OP Superchain mainnets, Ethereum, Arbitrum One, Base and Blast. It does not use private keys or submit transactions.

```bash
uv sync
uv run evm-inventory scan --wallets wallets.txt --db inventory.sqlite --delay-min 1 --delay-max 3
uv run evm-inventory export --run RUN_ID --db inventory.sqlite --output reports/RUN_ID
```

The CLI automatically loads an optional `.env` file from the current directory. Entries use literal `KEY=value` syntax; comments and an `export` prefix are accepted, and existing environment variables take precedence. Use `--interval` for the minimum request interval, or `--no-discovery` to disable optional Alchemy discovery.

The wallet file contains one public `0x` address per line. `--dry-run` validates the file and reports the planned check count without network requests. Results are resumable with `resume --run RUN_ID`. Optional additional token discovery is enabled only when `ALCHEMY_API_KEY` is present and `--no-discovery` is not supplied; the mandatory native/stablecoin RPC pass does not require it.

Public RPC endpoints can rate-limit or be unavailable. The reports distinguish zero balances, errors and unverified coverage. The packaged catalog is a dated snapshot and does not claim exhaustive ERC-20 discovery. Reading balances has no gas cost; the future collection phase will be a separate transaction-signing feature.

See [network and token coverage](docs/coverage.md) for current gaps. The default catalog has 35 networks, 36 stablecoin contracts, and 13 verified Alchemy Portfolio mappings. For 150 wallets this means 10,650 mandatory balance checks, plus additional discovered tokens. A balance check can require several HTTP requests; the dry-run number is not an HTTP request or CU estimate. Prices may be unavailable.

Discovery continues when a public RPC is unavailable. Its positive token candidates are saved with their provider-reported raw balance, then verified via RPC. Only successfully verified balances enter `balances.csv`; unverified candidates and errors remain in `inventory.json` and coverage reports. RPC and Alchemy transient failures trigger a five-minute cooldown after bounded retries. Authentication failures stop that endpoint/provider for the current process.

`checks.csv` includes every expected mandatory check, including checks not reached before an interruption. `coverage.csv` separates mandatory coverage, catalog review and discovery status. Progress and the run ID appear on stderr; the final summary is JSON on stdout.

A run uses its original catalog snapshot on `resume`. To use updated Alchemy mappings or a changed token catalog, start a new `scan`; old observations remain available under the old run ID. The same database can contain multiple runs. Exporting an interrupted run is safe and does not restart it.
