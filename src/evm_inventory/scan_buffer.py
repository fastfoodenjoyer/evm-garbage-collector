"""A sealed memory snapshot for scanner workers; SQLite stays on the caller thread."""

from copy import deepcopy
from threading import RLock

from .stages import StageDeadlineExceeded


class ScanBuffer:
    def __init__(self, wallet, chain_id, assets, state, deadline):
        self.wallet, self.chain_id = wallet, chain_id
        self.rows = {row["id"]: deepcopy(row) for row in assets}
        self.state = deepcopy(state)
        self.deadline = deadline
        self.operations = []
        self.lock = RLock()
        self.sealed = False
        self.next_id = -1

    def _check(self):
        if self.sealed:
            raise StageDeadlineExceeded("scan buffer sealed")
        self.deadline.check()

    def assets(self, wallet=None, chain_id=None):
        with self.lock:
            return deepcopy(list(self.rows.values()))

    def upsert_asset(self, wallet, chain_id, asset_id, kind, metadata):
        with self.lock:
            self._check()
            address = asset_id.lower()
            existing = next((r for r in self.rows.values() if r["asset_id"] == address), None)
            row_id = existing["id"] if existing else self.next_id
            if existing is None:
                self.next_id -= 1
                self.rows[row_id] = {
                    "id": row_id,
                    "asset_id": address,
                    "status": "pending",
                    "result": None,
                    "retry_after": None,
                }
            self.rows[row_id].update(kind=kind, metadata=deepcopy(metadata))
            self.operations.append(
                ("upsert_asset", (wallet, chain_id, asset_id, kind, deepcopy(metadata)), row_id)
            )
            return row_id

    def record_asset(self, row_id, result, status="success", retry_after=None):
        with self.lock:
            self._check()
            self.rows[row_id].update(
                result=deepcopy(result), status=status, retry_after=retry_after
            )
            self.operations.append(
                ("record_asset", (row_id, deepcopy(result), status, retry_after))
            )

    def discovery_state(self, wallet, chain_id):
        with self.lock:
            return deepcopy(self.state)

    def write_discovery_state(self, wallet, chain_id, **kwargs):
        with self.lock:
            self._check()
            self.state = {
                "cursor": None,
                "cursor_history": [],
                "status": "pending",
                "result": None,
                "error": None,
                "retry_after": None,
                **deepcopy(kwargs),
            }
            self.operations.append(("write_discovery_state", (wallet, chain_id), deepcopy(kwargs)))

    def save_pass(self, wallet, chain_id, block):
        with self.lock:
            self._check()
            self.operations.append(("save_pass", (wallet, chain_id, deepcopy(block))))

    def persist(self, store):
        """Seal before replaying so late workers cannot change the saved snapshot."""
        with self.lock:
            self.sealed = True
            operations = deepcopy(self.operations)
        ids = {}
        discovered = set()
        for operation in operations:
            method, args, *extra = operation
            if method == "upsert_asset":
                row_id = store.upsert_asset(*args)
                ids[extra[0]] = row_id
                discovered.add(row_id)
            elif method == "record_asset":
                row_id, *values = args
                store.record_asset(ids.get(row_id, row_id), *values)
            elif method == "write_discovery_state":
                store.write_discovery_state(*args, **extra[0])
            else:
                store.save_pass(*args)
        return discovered
