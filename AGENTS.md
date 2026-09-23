# Project workflow

- Do not commit documentation files, except `README.md`. Keep design
  specifications, implementation plans, and other project documents as local,
  ignored working artifacts.
- Use one project SQLite database for all operational runs: inventory scans,
  route plans and execution state, and Rabby DeFi plans and withdrawals. Reuse
  the same `--db` path across commands. Never create a separate SQLite journal
  or a per-run database. Human-review JSON exports may remain separate files.
