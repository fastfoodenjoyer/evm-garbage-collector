# EVM Inventory

Read-only inventory of native coins and a curated stablecoin list across OP Superchain mainnets, Ethereum, Arbitrum One, Base and Blast. It does not use private keys or submit transactions.

```bash
uv sync
uv run evm-inventory scan --wallets wallets.txt --db inventory.sqlite --delay-min 1 --delay-max 3
uv run evm-inventory export --run RUN_ID --db inventory.sqlite --output reports/RUN_ID
```

The wallet file contains one public `0x` address per line. `--dry-run` validates the file and reports the planned check count without network requests. Results are resumable with `resume --run RUN_ID`. Optional additional token discovery is enabled only when `ALCHEMY_API_KEY` is exported; the mandatory native/stablecoin RPC pass does not require it.

Public RPC endpoints can rate-limit or be unavailable. The reports distinguish zero balances, errors and unverified coverage. The packaged catalog is a dated snapshot and does not claim exhaustive ERC-20 discovery. Reading balances has no gas cost; the future collection phase will be a separate transaction-signing feature.
