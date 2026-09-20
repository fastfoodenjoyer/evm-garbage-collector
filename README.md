# EVM Inventory and Bitget Consolidation

Inventory of native coins and a curated stablecoin list across OP Superchain mainnets,
Ethereum, Arbitrum One, Base and Blast, followed by an explicit-only route executor.
Scanning and route quotation are read-only. The executor is the only command that can
sign or broadcast, and it requires `--execute`.

```bash
uv sync
uv run evm-inventory scan --wallets wallets.txt --db inventory.sqlite --delay-min 1 --delay-max 3
uv run evm-inventory export --run RUN_ID --db inventory.sqlite --output reports/RUN_ID
```

The CLI automatically loads an optional `.env` file from the current directory. Entries use literal `KEY=value` syntax; comments and an `export` prefix are accepted, and existing environment variables take precedence. Use `--interval` for the minimum request interval, or `--no-discovery` to disable optional Alchemy discovery.

The wallet file contains one public `0x` address per line. `--dry-run` validates the file and reports the planned check count without network requests. Results are resumable with `resume --run RUN_ID`. Optional additional token discovery is enabled only when `ALCHEMY_API_KEY` is present and `--no-discovery` is not supplied; the mandatory native/stablecoin RPC pass does not require it.

Create the operation workbook with `uv run evm-inventory workbook-template --output local/wallets.xlsx`. Its `Wallets` sheet has columns for a row number, public address, private key, Bitget deposit address, and proposed actions. The deposit address must be the correct Bitget EVM address for the asset and network selected for that row. `workbook-dry-run` validates the sheet without signing or submitting transactions. Private keys remain in memory only and are never printed or stored in the inventory database.

After an inventory export, make a live but read-only plan:

```bash
uv run evm-inventory quote-routes \
  --balances local/fresh-report/balances.csv \
  --workbook local/wallets.xlsx \
  --output local/route-plan.json
```

`quote-routes` downloads Bitget's public coin catalog at planning time. It accepts only
enabled EVM deposit networks, checks the exact minimum deposit in native token units,
uses the strict `config/swap-allowlist.json`, and requests cheapest routes through
`https://api.jumper.xyz/pipeline/v1/advanced/routes`. Subminimum positions are routed
to wallet-owned USDC on Base, then included in a single final transfer only when their
combined `toAmountMin` meets Bitget's current minimum. Amounts below 0.01 tokens are
classified as dust before a route request. Native-asset routes are requoted after
reserving five times their quoted source-chain gas cost.

To broadcast the saved plan, use the separate command below. It processes wallets in
order, waits a random 30–180 minutes between wallets, signs only with the matching
workbook key, verifies the 5× native-gas reserve, uses exact ERC-20 approvals when a
Jumper step requires one, waits for the source-chain receipt, and records each action
in the SQLite journal. It then polls the signed Bitget deposit API for the resulting
transaction hash. This requires `BITGET_API_KEY`, `BITGET_SECRET_KEY`, and
`BITGET_PASSPHRASE` in `.env`.

```bash
uv run evm-inventory execute-routes \
  --plan local/route-plan.json \
  --workbook local/wallets.xlsx \
  --catalog local/rpc-allowlist-final-catalog.json \
  --journal local/execution-journal.sqlite \
  --execute
```

Public RPC endpoints can rate-limit or be unavailable. The reports distinguish zero balances, errors and unverified coverage. The packaged catalog is a dated snapshot and does not claim exhaustive ERC-20 discovery. Reading balances has no gas cost; the future collection phase will be a separate transaction-signing feature.

See [network and token coverage](docs/coverage.md) for current gaps. The default catalog has 35 networks, 36 stablecoin contracts, and 13 verified Alchemy Portfolio mappings. For 150 wallets this means 10,650 mandatory balance checks, plus additional discovered tokens. A balance check can require several HTTP requests; the dry-run number is not an HTTP request or CU estimate. Prices may be unavailable.

`config/swap-allowlist.json` is the future transaction allowlist. It defaults to `deny` and matches an asset only by exact `chain_id` plus contract address (or `native`). `swap` assets may be routed, `unwrap` assets may only be unwrapped to the native coin, and `review` assets require an explicit routing decision. The inventory scanner never signs transactions.

Discovery continues when a public RPC is unavailable. Its positive token candidates are saved with their provider-reported raw balance, then verified via RPC. Only successfully verified balances enter `balances.csv`; unverified candidates and errors remain in `inventory.json` and coverage reports. RPC and Alchemy transient failures trigger a five-minute cooldown after bounded retries. Authentication failures stop that endpoint/provider for the current process.

`checks.csv` includes every expected mandatory check, including checks not reached before an interruption. `coverage.csv` separates mandatory coverage, catalog review and discovery status. Progress and the run ID appear on stderr; the final summary is JSON on stdout.

A run uses its original catalog snapshot on `resume`. To use updated Alchemy mappings or a changed token catalog, start a new `scan`; old observations remain available under the old run ID. The same database can contain multiple runs. Exporting an interrupted run is safe and does not restart it.
