"""The DuckDB side: load sources, lock the database down, keep the catalog fresh."""

from __future__ import annotations

import fnmatch
import json
import logging
import shutil
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from . import catalog, rest
from . import types as t
from .config import Config, Source, User

log = logging.getLogger("pgfederate")
META_SCHEMA = "pgfederate"
FORMATS = {
    ".csv": "read_csv",
    ".tsv": "read_csv",
    ".parquet": "read_parquet",
    ".json": "read_json",
    ".jsonl": "read_json",
    ".ndjson": "read_json",
}
EXTENSIONS = {"postgres": "postgres", "mysql": "mysql", "sqlite": "sqlite"}
DEFAULT_SCHEMAS = {"postgres": ["public"], "sqlite": ["main"]}

POSTGRES_FOREIGN_KEYS = """
SELECT n.nspname AS s, c.relname AS t, a.attname AS c, fn.nspname AS rs, fc.relname AS rt, fa.attname AS rc
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class fc ON fc.oid = k.confrelid
JOIN pg_namespace fn ON fn.oid = fc.relnamespace
JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1]
JOIN pg_attribute fa ON fa.attrelid = k.confrelid AND fa.attnum = k.confkey[1]
WHERE k.contype = 'f' AND array_length(k.conkey, 1) = 1
"""


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass
class SourceStatus:
    name: str
    type: str
    status: str = "pending"
    tables: int = 0
    rows: int | None = None
    last_refresh: float | None = None
    duration: float | None = None
    error: str | None = None


class Engine:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db: Any = None
        self.tmp = Path(tempfile.mkdtemp(prefix="pgfederate-"))
        self.status = {s.name: SourceStatus(s.name, s.type) for s in config.sources}
        self.exposed: dict[str, dict[str, str]] = {s.name: {} for s in config.sources}
        self.foreign_keys: list[catalog.ForeignKey] = []
        self.lock = threading.Lock()
        self.locked_down = False

    # lifecycle -----------------------------------------------------------

    def start(self) -> None:
        options: dict[str, Any] = {"allow_community_extensions": False}
        if self.config.memory_limit:
            options["memory_limit"] = self.config.memory_limit
        self.db = duckdb.connect(":memory:", config=options)
        self.db.execute("SET python_enable_replacements = false")
        self.db.execute(f"CREATE SCHEMA {META_SCHEMA}")
        catalog.create_tables(self.db, "__pg")
        for macro in catalog.MACROS:
            self.db.execute(macro)
        self._attach_all()
        for source in self.config.sources:
            self.db.execute(f"CREATE SCHEMA IF NOT EXISTS {quote(source.name)}")
        self._lock_down()
        for source in self.config.sources:
            self.refresh(source, publish=False)
        self.publish_catalog()

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cursor(self) -> Any:
        return self.db.cursor()

    def _attach_all(self) -> None:
        for source in self.config.sources:
            if source.type not in EXTENSIONS:
                continue
            extension = EXTENSIONS[source.type]
            self.db.execute(f"INSTALL {extension}")
            self.db.execute(f"LOAD {extension}")
            target = source.options.get("dsn") or source.options.get("path")
            if not target:
                raise ValueError(f"source {source.name}: needs dsn or path")
            if source.type == "sqlite":
                target = str(self._resolve(target))
            self.db.execute(
                f"ATTACH {literal(target)} AS {quote('__src_' + source.name)} "
                f"(TYPE {source.type}, READ_ONLY)"
            )

    def _lock_down(self) -> None:
        """From here on SQL can reach only the attached sources and listed paths."""
        allowed = [str(self.tmp)]
        for source in self.config.sources:
            if source.type != "files":
                continue
            for spec in self._file_tables(source).values():
                if not spec["path"].startswith(("http://", "https://")):
                    allowed.append(str(self._glob_root(spec["path"])))
        listing = "[" + ", ".join(literal(path) for path in sorted(set(allowed))) + "]"
        self.db.execute(f"SET allowed_directories = {listing}")
        self.db.execute("SET enable_external_access = false")
        self.locked_down = True

    def _resolve(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        return candidate if candidate.is_absolute() else self.config.base_dir / candidate

    def _glob_root(self, path: str) -> Path:
        parts = []
        for part in Path(path).parts:
            if any(char in part for char in "*?["):
                break
            parts.append(part)
        root = Path(*parts) if parts else Path(".")
        return root if root.is_dir() else root.parent

    # sources ---------------------------------------------------------------

    def refresh(self, source: Source, publish: bool = True) -> None:
        status = self.status[source.name]
        started = time.time()
        try:
            cursor = self.cursor()
            loader = {
                "postgres": self._load_database,
                "mysql": self._load_database,
                "sqlite": self._load_database,
                "files": self._load_files,
                "rest": self._load_rest,
            }[source.type]
            with self.lock:
                rows = loader(source, cursor)
            status.status, status.error, status.rows = "ok", None, rows
            status.tables = len(self.exposed[source.name])
            status.last_refresh = time.time()
        except Exception as error:  # keep serving the last good data
            log.error("source %s: refresh failed: %s", source.name, error)
            status.status, status.error = "error", str(error)
        status.duration = time.time() - started
        if publish:
            self.publish_catalog()

    def _load_database(self, source: Source, cursor: Any) -> None:
        attached = "__src_" + source.name
        if source.type == "postgres":
            cursor.execute("CALL postgres_clear_cache()")
        elif source.type == "mysql":
            cursor.execute("CALL mysql_clear_cache()")
        wanted = source.options.get("schemas") or DEFAULT_SCHEMAS.get(source.type)
        include = source.options.get("include") or ["*"]
        exclude = source.options.get("exclude") or []
        found = cursor.execute(
            """SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = ?
               UNION SELECT schema_name, view_name FROM duckdb_views()
               WHERE database_name = ? AND NOT internal ORDER BY 1, 2""",
            [attached, attached],
        ).fetchall()
        schemas = {schema for schema, _ in found}
        multiple = wanted is not None and len(wanted) > 1 or wanted is None and len(schemas) > 1
        exposed: dict[str, str] = {}
        for schema, table in found:
            if wanted is not None and schema not in wanted:
                continue
            if not any(fnmatch.fnmatch(table, pattern) for pattern in include):
                continue
            if any(fnmatch.fnmatch(table, pattern) for pattern in exclude):
                continue
            name = f"{schema}__{table}" if multiple else table
            cursor.execute(
                f"CREATE OR REPLACE VIEW {quote(source.name)}.{quote(name)} AS "
                f"SELECT * FROM {quote(attached)}.{quote(schema)}.{quote(table)}"
            )
            exposed[name] = f"{schema}.{table}"
        for stale in set(self.exposed[source.name]) - set(exposed):
            cursor.execute(f"DROP VIEW IF EXISTS {quote(source.name)}.{quote(stale)}")
        self.exposed[source.name] = exposed
        self.foreign_keys = [k for k in self.foreign_keys if k.schema != source.name]
        if source.type == "postgres":
            self._load_postgres_foreign_keys(source, cursor, exposed)

    def _load_postgres_foreign_keys(self, source: Source, cursor: Any, exposed: dict[str, str]) -> None:
        names = {origin: name for name, origin in exposed.items()}
        query = f"SELECT * FROM postgres_query({literal('__src_' + source.name)}, {literal(POSTGRES_FOREIGN_KEYS)})"
        for schema, table, column, ref_schema, ref_table, ref_column in cursor.execute(query).fetchall():
            left, right = names.get(f"{schema}.{table}"), names.get(f"{ref_schema}.{ref_table}")
            if left and right:
                self.foreign_keys.append(
                    catalog.ForeignKey(source.name, left, column, source.name, right, ref_column)
                )

    def _file_tables(self, source: Source) -> dict[str, dict[str, Any]]:
        tables = {}
        for name, spec in (source.options.get("tables") or {}).items():
            spec = {"path": spec} if isinstance(spec, str) else dict(spec)
            if not spec["path"].startswith(("http://", "https://")):
                spec["path"] = str(self._resolve(spec["path"]))
            tables[name] = spec
        return tables

    def _reader(self, spec: dict[str, Any], path: str) -> str:
        format_name = spec.get("format")
        function = f"read_{format_name}" if format_name else None
        if function is None:
            suffix = Path(spec["path"].split("?")[0]).suffix.lower()
            if suffix not in FORMATS:
                raise ValueError(f"cannot infer the format of {spec['path']}, set format:")
            function = FORMATS[suffix]
        return f"{function}({literal(path)})"

    def _load_files(self, source: Source, cursor: Any) -> int:
        schema = quote(source.name)
        exposed: dict[str, str] = {}
        rows = 0
        for name, spec in self._file_tables(source).items():
            path = spec["path"]
            target = f"{schema}.{quote(name)}"
            if path.startswith(("http://", "https://")):
                local = self.tmp / f"{source.name}-{name}.download"
                request = urllib.request.Request(path, headers={"User-Agent": "pgfederate"})
                with urllib.request.urlopen(request, timeout=60) as response, local.open("wb") as out:
                    shutil.copyfileobj(response, out)
                cursor.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {self._reader(spec, str(local))}")
                local.unlink()
                rows += cursor.execute(f"SELECT count(*) FROM {target}").fetchone()[0]
            else:
                cursor.execute(f"CREATE OR REPLACE VIEW {target} AS SELECT * FROM {self._reader(spec, path)}")
            exposed[name] = path
        self.exposed[source.name] = exposed
        return rows

    def _load_rest(self, source: Source, cursor: Any) -> int:
        options = source.options
        headers = {key: str(value) for key, value in (options.get("headers") or {}).items()}
        total = 0
        exposed = dict(self.exposed[source.name])
        for name, table in (options.get("tables") or {}).items():
            spool = self.tmp / f"{source.name}-{name}.ndjson"
            count = 0
            with spool.open("w", encoding="utf-8") as out:
                for record in rest.fetch_records(options.get("base_url", ""), table, headers):
                    out.write(json.dumps(record, default=str) + "\n")
                    count += 1
            target = f"{quote(source.name)}.{quote(name)}"
            columns = table.get("columns") or {}
            if count:
                select = "*"
                if columns:
                    select = ", ".join(f"CAST({quote(c)} AS {kind}) AS {quote(c)}" for c, kind in columns.items())
                reader = f"read_json({literal(str(spool))}, format = 'newline_delimited', sample_size = -1)"
                cursor.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT {select} FROM {reader}")
            elif columns:
                definition = ", ".join(f"{quote(c)} {kind}" for c, kind in columns.items())
                cursor.execute(f"CREATE OR REPLACE TABLE {target} ({definition})")
            elif name not in exposed:
                cursor.execute(f"CREATE TABLE {target} (empty_response VARCHAR)")
            spool.unlink()
            exposed[name] = table.get("path", "")
            total += count
        self.exposed[source.name] = exposed
        return total

    # catalog -----------------------------------------------------------------

    def model(self, user: User | None = None) -> catalog.Model:
        allowed = None if user is None or user.sources is None else set(user.sources)
        cursor = self.cursor()
        schemas = [s.name for s in self.config.sources if allowed is None or s.name in allowed]
        if allowed is None:
            schemas.append(META_SCHEMA)
        rows = cursor.execute(
            """SELECT table_schema, table_name, column_name, data_type, is_nullable
               FROM information_schema.columns
               WHERE table_catalog = current_database() AND table_schema IN (SELECT unnest(?))
               ORDER BY table_schema, table_name, ordinal_position""",
            [schemas],
        ).fetchall()
        tables: dict[tuple[str, str], catalog.Table] = {}
        for schema, table, column, kind, nullable in rows:
            entry = tables.setdefault((schema, table), catalog.Table(schema, table, []))
            entry.columns.append(catalog.Column(column, t.oid_for(kind), not_null=nullable == "NO"))
        self._apply_primary_keys(cursor, tables)
        keys = [k for k in self._all_foreign_keys() if k.schema in schemas and k.ref_schema in schemas]
        return catalog.Model(self.config.database, list(tables.values()), keys, schemas)

    def _apply_primary_keys(self, cursor: Any, tables: dict[tuple[str, str], catalog.Table]) -> None:
        for source in self.config.sources:
            if source.type not in EXTENSIONS:
                continue
            constraints = cursor.execute(
                """SELECT schema_name, table_name, constraint_column_names FROM duckdb_constraints()
                   WHERE database_name = ? AND constraint_type = 'PRIMARY KEY'""",
                ["__src_" + source.name],
            ).fetchall()
            names = {origin: name for name, origin in self.exposed[source.name].items()}
            for schema, table, columns in constraints:
                name = names.get(f"{schema}.{table}")
                if name and (source.name, name) in tables:
                    tables[(source.name, name)].primary_key = list(columns)
        for (schema, table), columns in self.config.primary_keys.items():
            if (schema, table) in tables:
                tables[(schema, table)].primary_key = columns

    def _all_foreign_keys(self) -> list[catalog.ForeignKey]:
        declared = [
            catalog.ForeignKey(r.from_schema, r.from_table, r.from_column, r.to_schema, r.to_table, r.to_column)
            for r in self.config.relations
        ]
        return self.foreign_keys + declared

    def publish_catalog(self) -> None:
        with self.lock:
            cursor = self.cursor()
            self._publish_sources(cursor)
            catalog.publish(cursor, "__pg", self.model())
            for user in self.config.users:
                if user.sources is not None:
                    schema = self.catalog_schema(user)
                    catalog.create_tables(cursor, schema)
                    catalog.publish(cursor, schema, self.model(user))

    def _publish_sources(self, cursor: Any) -> None:
        cursor.execute(
            f"""CREATE OR REPLACE TABLE {META_SCHEMA}.sources (
                name VARCHAR, type VARCHAR, status VARCHAR, tables INTEGER, rows BIGINT,
                last_refresh TIMESTAMPTZ, duration_seconds DOUBLE, error VARCHAR)"""
        )
        for item in self.status.values():
            cursor.execute(
                f"INSERT INTO {META_SCHEMA}.sources VALUES (?, ?, ?, ?, ?, to_timestamp(?), ?, ?)",
                [item.name, item.type, item.status, item.tables, item.rows,
                 item.last_refresh, item.duration, item.error],
            )

    @staticmethod
    def catalog_schema(user: User | None) -> str:
        if user is None or user.sources is None:
            return "__pg"
        return "__pg_" + "".join(c if c.isalnum() else "_" for c in user.name)
