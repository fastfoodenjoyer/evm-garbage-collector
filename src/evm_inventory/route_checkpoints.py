"""Key-free JSON snapshots of prepared routes, used only to observe saved hashes."""

from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from . import consolidation, lifi, models, valuation

_TYPES = {
    cls.__name__: cls
    for module in (consolidation, lifi, models, valuation)
    for cls in vars(module).values()
    if isinstance(cls, type) and is_dataclass(cls)
}


def encode(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, timedelta):
        return {"$timedelta": value.total_seconds()}
    if isinstance(value, tuple):
        return {"$tuple": [encode(item) for item in value]}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {
            key: encode(item)
            for key, item in value.items()
            if not callable(item)
            and key not in {"private_key", "secret_key", "api_key", "passphrase"}
        }
    if is_dataclass(value) and type(value).__name__ in _TYPES:
        return {
            "$type": type(value).__name__,
            "fields": {field.name: encode(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, SimpleNamespace):
        return {"$namespace": encode(vars(value))}
    raise ValueError(f"unsupported route checkpoint value: {type(value).__name__}")


def decode(value):
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$decimal" in value:
        return Decimal(value["$decimal"])
    if "$datetime" in value:
        return datetime.fromisoformat(value["$datetime"])
    if "$timedelta" in value:
        return timedelta(seconds=value["$timedelta"])
    if "$tuple" in value:
        return tuple(decode(item) for item in value["$tuple"])
    if "$namespace" in value:
        return SimpleNamespace(**decode(value["$namespace"]))
    if "$type" in value:
        return _TYPES[value["$type"]](**decode(value["fields"]))
    return {key: decode(item) for key, item in value.items()}
