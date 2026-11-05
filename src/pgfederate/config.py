"""YAML configuration: loading, ${VAR} interpolation and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SOURCE_TYPES = {"postgres", "mysql", "sqlite", "files", "rest"}
RESERVED_SCHEMAS = {"main", "pg_catalog", "information_schema", "pgfederate", "__pg", "temp"}
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
DURATION = re.compile(r"^(\d+)(s|m|h|d)$")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """The configuration file is invalid."""


def parse_duration(value: str | int | None) -> int | None:
    """Return seconds for values such as 30s, 15m, 2h or 1d."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    match = DURATION.match(str(value))
    if not match:
        raise ConfigError(f"invalid duration {value!r}, expected e.g. 30s, 15m, 2h, 1d")
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return int(match.group(1)) * unit


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is not set")

        return ENV_REF.sub(replace, value)
    if isinstance(value, list):
        return [_interpolate(item) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate(item) for key, item in value.items()}
    return value


@dataclass
class User:
    name: str
    password: str
    sources: list[str] | None = None  # None means every source


@dataclass
class Source:
    name: str
    type: str
    options: dict[str, Any]
    refresh: int | None = None


@dataclass
class Relation:
    """A foreign key shown to clients, possibly across two sources."""

    from_schema: str
    from_table: str
    from_column: str
    to_schema: str
    to_table: str
    to_column: str


@dataclass
class Config:
    sources: list[Source]
    host: str = "127.0.0.1"
    port: int = 5433
    database: str = "pgfederate"
    metrics_port: int | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    memory_limit: str | None = None
    users: list[User] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    primary_keys: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)


def _split_column(path: str) -> tuple[str, str, str]:
    parts = path.split(".")
    if len(parts) != 3:
        raise ConfigError(f"relation endpoint {path!r} must be source.table.column")
    return parts[0], parts[1], parts[2]


def parse(data: dict[str, Any], base_dir: Path | None = None) -> Config:
    data = _interpolate(data or {})
    known = {"server", "users", "sources", "relations", "primary_keys"}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown top-level keys: {sorted(unknown)}")

    server = data.get("server") or {}
    tls = server.get("tls") or {}
    if bool(tls.get("cert")) != bool(tls.get("key")):
        raise ConfigError("server.tls needs both cert and key")

    raw_sources = data.get("sources") or []
    if not raw_sources:
        raise ConfigError("at least one source is required")
    sources: list[Source] = []
    seen: set[str] = set()
    for raw in raw_sources:
        options = dict(raw)
        name = options.pop("name", None)
        kind = options.pop("type", None)
        if not name or not IDENTIFIER.match(name):
            raise ConfigError(f"source name {name!r} must match {IDENTIFIER.pattern}")
        if name in RESERVED_SCHEMAS or name.startswith("__") or name.startswith("pg_"):
            raise ConfigError(f"source name {name!r} is reserved")
        if name in seen:
            raise ConfigError(f"duplicate source name {name!r}")
        if kind not in SOURCE_TYPES:
            raise ConfigError(f"source {name}: type must be one of {sorted(SOURCE_TYPES)}")
        seen.add(name)
        refresh = parse_duration(options.pop("refresh", None))
        sources.append(Source(name, kind, options, refresh))

    users = []
    for raw in data.get("users") or []:
        if not raw.get("name") or not raw.get("password"):
            raise ConfigError("every user needs a name and a password")
        allowed = raw.get("sources")
        for source in allowed or []:
            if source not in seen:
                raise ConfigError(f"user {raw['name']}: unknown source {source!r}")
        users.append(User(raw["name"], str(raw["password"]), allowed))

    relations = []
    for raw in data.get("relations") or []:
        if "from" not in raw or "to" not in raw:
            raise ConfigError("every relation needs from and to")
        left, right = _split_column(raw["from"]), _split_column(raw["to"])
        for schema in (left[0], right[0]):
            if schema not in seen:
                raise ConfigError(f"relation uses unknown source {schema!r}")
        relations.append(Relation(*left, *right))

    primary_keys: dict[tuple[str, str], list[str]] = {}
    for table, columns in (data.get("primary_keys") or {}).items():
        parts = table.split(".")
        if len(parts) != 2 or parts[0] not in seen:
            raise ConfigError(f"primary_keys entry {table!r} must be source.table")
        primary_keys[(parts[0], parts[1])] = columns if isinstance(columns, list) else [columns]

    return Config(
        sources=sources,
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", 5433)),
        database=server.get("database", "pgfederate"),
        metrics_port=server.get("metrics_port"),
        tls_cert=tls.get("cert"),
        tls_key=tls.get("key"),
        memory_limit=server.get("memory_limit"),
        users=users,
        relations=relations,
        primary_keys=primary_keys,
        base_dir=base_dir or Path.cwd(),
    )


def load(path: str | Path) -> Config:
    path = Path(path)
    with path.open(encoding="utf-8") as handle:
        return parse(yaml.safe_load(handle), path.resolve().parent)
