"""The asyncio server: one Session per client connection."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import ssl
import struct
import time
from dataclasses import dataclass, field
from typing import Any

import duckdb
from sqlglot import exp

from . import __version__, pgwire as w, sql
from . import types as t
from .config import Config, User
from .engine import META_SCHEMA, Engine

log = logging.getLogger("pgfederate")
FETCH_SIZE = 2000
MAX_MESSAGE = 64 * 1024 * 1024
MAX_LOGIN_MESSAGE = 8192
SERVER_VERSION = f"16.0 (pgfederate {__version__})"
STATUS_SETTINGS = {
    "application_name": "",
    "client_encoding": "UTF8",
    "DateStyle": "ISO, MDY",
    "IntervalStyle": "postgres",
    "TimeZone": "UTC",
    "integer_datetimes": "on",
    "is_superuser": "off",
    "server_encoding": "UTF8",
    "server_version": SERVER_VERSION,
    "standard_conforming_strings": "on",
}
SHOW_DEFAULTS = {
    "transaction isolation level": "read committed",
    "transaction_isolation": "read committed",
    "default_transaction_isolation": "read committed",
    "transaction_read_only": "on",
    "default_transaction_read_only": "on",
    "search_path": '"$user", public',
    "max_identifier_length": "63",
    "lc_collate": "C",
    "lc_ctype": "C",
    "max_index_keys": "32",
    "server_version_num": "160000",
}
SQLSTATE = {
    duckdb.CatalogException: "42P01",
    duckdb.BinderException: "42703",
    duckdb.ParserException: "42601",
    duckdb.ConversionException: "22P02",
    duckdb.InterruptException: "57014",
    duckdb.PermissionException: "42501",
    duckdb.OutOfMemoryException: "53200",
}


def sqlstate(error: Exception) -> str:
    for kind, code in SQLSTATE.items():
        if isinstance(error, kind):
            return code
    return "XX000"


class ClientGone(Exception):
    pass


@dataclass
class Prepared:
    statement: sql.Statement
    param_types: list[int]


@dataclass
class Portal:
    prepared: Prepared
    result_formats: list[int]
    params: dict[int, exp.Expression]
    cursor: Any = None
    columns: list[tuple[str, int]] | None = None
    buffer: list[Any] = field(default_factory=list)
    done: bool = False
    sent: int = 0
    canned: list[tuple[Any, ...]] | None = None


class Server:
    def __init__(self, config: Config, engine: Engine) -> None:
        self.config = config
        self.engine = engine
        self.users = {user.name: user for user in config.users}
        self.sessions: dict[int, "Session"] = {}
        self.next_pid = 1000
        self.connections_total = 0
        self.queries_total = 0
        self.errors_total = 0
        self.started = time.time()
        self.tls: ssl.SSLContext | None = None
        if config.tls_cert:
            self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.tls.load_cert_chain(config.tls_cert, config.tls_key)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = Session(self, reader, writer)
        try:
            await session.run()
        except (ClientGone, asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            log.exception("connection failed")
        finally:
            session.close()
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass

    def table_index(self, user: User | None) -> dict[str, list[str]]:
        allowed = self.allowed_schemas(user)
        index: dict[str, list[str]] = {}
        for schema, tables in self.engine.exposed.items():
            if schema in allowed:
                for table in tables:
                    index.setdefault(table, []).append(schema)
        return index

    def allowed_schemas(self, user: User | None) -> set[str]:
        names = {source.name for source in self.config.sources}
        if user is not None and user.sources is not None:
            return names & set(user.sources)  # source status can name hosts and URLs
        return names | {META_SCHEMA}


class Session:
    def __init__(self, server: Server, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.user: User | None = None
        self.user_name = "pgfederate"
        self.settings: dict[str, str] = {k.lower(): v for k, v in STATUS_SETTINGS.items()}
        self.settings.update(SHOW_DEFAULTS)
        self.prepared: dict[str, Prepared] = {}
        self.portals: dict[str, Portal] = {}
        self.tx = b"I"
        self.failed_extended = False
        self.active: Any = None
        server.next_pid += 1
        self.pid = server.next_pid
        self.secret = secrets.randbits(31)
        self.context: sql.Context | None = None

    def close(self) -> None:
        self.server.sessions.pop(self.pid, None)
        self.cancel()
        for portal in self.portals.values():
            if portal.cursor is not None:
                portal.cursor.close()

    # plumbing -----------------------------------------------------------------

    def send(self, data: bytes) -> None:
        self.writer.write(data)

    async def flush(self) -> None:
        await self.writer.drain()

    async def read_startup(self) -> bytes:
        header = await self.reader.readexactly(4)
        length = struct.unpack("!I", header)[0]
        if not 8 <= length <= 10240:
            raise ClientGone
        return await self.reader.readexactly(length - 4)

    async def read_message(self, limit: int = MAX_MESSAGE) -> tuple[bytes, bytes]:
        header = await self.reader.readexactly(5)
        length = struct.unpack("!I", header[1:])[0]
        if not 4 <= length <= limit:
            raise ClientGone
        return header[:1], await self.reader.readexactly(length - 4)

    # startup --------------------------------------------------------------------

    async def run(self) -> None:
        if not await self.startup():
            return
        self.server.connections_total += 1
        self.server.sessions[self.pid] = self
        while True:
            kind, body = await self.read_message()
            if kind == b"X":
                return
            await self.dispatch(kind, body)
            await self.flush()

    async def startup(self) -> bool:
        while True:
            payload = await self.read_startup()
            code = struct.unpack("!I", payload[:4])[0]
            if code == 80877103:  # SSLRequest
                if self.server.tls is None:
                    self.send(b"N")
                    await self.flush()
                    continue
                self.send(b"S")
                await self.flush()
                await self.writer.start_tls(self.server.tls)
                continue
            if code == 80877104:  # GSSENCRequest
                self.send(b"N")
                await self.flush()
                continue
            if code == 80877102:  # CancelRequest
                pid, secret = struct.unpack("!II", payload[4:12])
                target = self.server.sessions.get(pid)
                if target is not None and secrets.compare_digest(str(target.secret), str(secret)):
                    target.cancel()
                return False
            if code != 196608:
                self.send(w.error_response("unsupported protocol version", "0A000", "FATAL"))
                await self.flush()
                return False
            break

        parts = payload[4:].split(b"\0")
        params = {parts[i].decode(): parts[i + 1].decode() for i in range(0, len(parts) - 1, 2) if parts[i]}
        name = params.get("user", "")
        database = params.get("database", name)
        config = self.server.config
        if database and database != config.database:
            log.debug("client asked for database %s", database)
        if self.server.users:
            if not await self.authenticate(name):
                return False
        self.user_name = name or "pgfederate"
        self.send(struct.pack("!cII", b"R", 8, 0))
        for key in ("application_name", "TimeZone"):
            if key in params:
                self.settings[key.lower()] = params[key]
        for key, value in STATUS_SETTINGS.items():
            self.send(w.parameter_status(key, self.settings.get(key.lower(), value)))
        self.send(w.message(b"K", struct.pack("!II", self.pid, self.secret)))
        self.send(w.ready(self.tx))
        await self.flush()
        return True

    async def authenticate(self, name: str) -> bool:
        user = self.server.users.get(name)
        # An unknown user runs the same exchange, so names cannot be probed.
        scram = w.Scram(user.password if user else secrets.token_hex(16))
        self.send(w.message(b"R", struct.pack("!I", 10) + b"SCRAM-SHA-256\0\0"))
        await self.flush()
        kind, body = await self.read_message(MAX_LOGIN_MESSAGE)
        if kind != b"p":
            return False
        mechanism_end = body.index(b"\0")
        length = struct.unpack("!i", body[mechanism_end + 1 : mechanism_end + 5])[0]
        client_first = body[mechanism_end + 5 : mechanism_end + 5 + length].decode()
        server_first = scram.first(client_first)
        self.send(w.message(b"R", struct.pack("!I", 11) + server_first.encode()))
        await self.flush()
        kind, body = await self.read_message(MAX_LOGIN_MESSAGE)
        if kind != b"p":
            return False
        verifier = scram.final(body.decode())
        if verifier is None or user is None:
            self.send(w.error_response(f'password authentication failed for user "{name}"', "28P01", "FATAL"))
            await self.flush()
            return False
        self.user = user
        self.send(w.message(b"R", struct.pack("!I", 12) + verifier.encode()))
        return True

    def cancel(self) -> None:
        cursor = self.active
        if cursor is not None:
            cursor.interrupt()

    def ctx(self) -> sql.Context:
        server = self.server
        return sql.Context(
            database=server.config.database,
            user=self.user_name,
            allowed=server.allowed_schemas(self.user),
            tables=server.table_index(self.user),
            catalog_schema=server.engine.catalog_schema(self.user),
            pid=self.pid,
            settings=self.settings,
            version=f"PostgreSQL {SERVER_VERSION}",
        )

    # message loop ---------------------------------------------------------------------

    async def dispatch(self, kind: bytes, body: bytes) -> None:
        if kind == b"Q":
            await self.simple_query(body[:-1].decode())
            return
        if kind == b"S":
            self.failed_extended = False
            self.send(w.ready(self.tx))
            return
        if kind == b"H":
            return
        if self.failed_extended:
            return
        try:
            if kind == b"P":
                self.parse_message(body)
            elif kind == b"B":
                self.bind_message(body)
            elif kind == b"D":
                await self.describe_message(body)
            elif kind == b"E":
                await self.execute_message(body)
            elif kind == b"C":
                self.close_message(body)
            else:
                raise sql.SqlError(f"unsupported message {kind!r}", "08P01")
        except Exception as error:
            self.report(error)
            self.failed_extended = True

    def report(self, error: Exception) -> None:
        self.server.errors_total += 1
        if self.tx == b"T":
            self.tx = b"E"
        code = error.code if isinstance(error, sql.SqlError) else sqlstate(error)
        text = str(error).split("\n")[0]
        log.info("query failed [%s]: %s", code, text)
        self.send(w.error_response(text, code))

    async def simple_query(self, text: str) -> None:
        try:
            statements = sql.parse(text, self.ctx())
            for statement in statements:
                prepared = Prepared(statement, [])
                portal = Portal(prepared, [], {})
                await self.start(portal)
                if portal.columns:
                    self.send(w.row_description(portal.columns))
                await self.stream(portal, 0)
        except Exception as error:
            self.report(error)
        self.send(w.ready(self.tx))

    def parse_message(self, body: bytes) -> None:
        name_end = body.index(b"\0")
        name = body[:name_end].decode()
        query_end = body.index(b"\0", name_end + 1)
        query = body[name_end + 1 : query_end].decode()
        count = struct.unpack("!H", body[query_end + 1 : query_end + 3])[0]
        declared = list(struct.unpack(f"!{count}I", body[query_end + 3 : query_end + 3 + 4 * count]))
        pieces = sql.split_statements(query)
        if len(pieces) > 1:
            raise sql.SqlError("cannot insert multiple commands into a prepared statement")
        statement = sql.parse_one(pieces[0], self.ctx()) if pieces else sql.Statement("empty")
        types = [t.TEXT] * statement.param_count
        if statement.tree is not None:
            types = sql.param_types(statement.tree, statement.param_count)
        for index, oid in enumerate(declared[: len(types)]):
            if oid:
                types[index] = oid
        self.prepared[name] = Prepared(statement, types)
        self.send(w.message(b"1"))

    def bind_message(self, body: bytes) -> None:
        portal_end = body.index(b"\0")
        portal_name = body[:portal_end].decode()
        statement_end = body.index(b"\0", portal_end + 1)
        statement_name = body[portal_end + 1 : statement_end].decode()
        prepared = self.prepared.get(statement_name)
        if prepared is None:
            raise sql.SqlError(f'prepared statement "{statement_name}" does not exist', "26000")
        offset = statement_end + 1
        (format_count,) = struct.unpack_from("!H", body, offset)
        offset += 2
        formats = list(struct.unpack_from(f"!{format_count}H", body, offset))
        offset += 2 * format_count
        (value_count,) = struct.unpack_from("!H", body, offset)
        offset += 2
        params: dict[int, exp.Expression] = {}
        for index in range(value_count):
            (length,) = struct.unpack_from("!i", body, offset)
            offset += 4
            data = None
            if length >= 0:
                data = body[offset : offset + length]
                offset += length
            code = formats[index] if len(formats) > 1 else (formats[0] if formats else 0)
            oid = prepared.param_types[index] if index < len(prepared.param_types) else t.TEXT
            params[index + 1] = w.parameter_literal(data, oid, code)
        (result_count,) = struct.unpack_from("!H", body, offset)
        offset += 2
        result_formats = list(struct.unpack_from(f"!{result_count}H", body, offset))
        old = self.portals.pop(portal_name, None)
        if old is not None and old.cursor is not None:
            old.cursor.close()
        self.portals[portal_name] = Portal(prepared, result_formats, params)
        self.send(w.message(b"2"))

    async def describe_message(self, body: bytes) -> None:
        target = body[:1]
        name = body[1:-1].decode()
        if target == b"S":
            prepared = self.prepared.get(name)
            if prepared is None:
                raise sql.SqlError(f'prepared statement "{name}" does not exist', "26000")
            types = prepared.param_types
            self.send(w.message(b"t", struct.pack("!H", len(types)) + b"".join(struct.pack("!I", o) for o in types)))
            columns = await self.describe_columns(prepared.statement)
            self.send(w.row_description(columns) if columns else w.message(b"n"))
            return
        portal = self.portals.get(name)
        if portal is None:
            raise sql.SqlError(f'portal "{name}" does not exist', "34000")
        await self.start(portal)
        if portal.columns:
            self.send(w.row_description(portal.columns, self.formats(portal)))
        else:
            self.send(w.message(b"n"))

    async def describe_columns(self, statement: sql.Statement) -> list[tuple[str, int]]:
        if statement.kind == "show":
            return [(statement.name, t.TEXT)]
        if statement.kind == "explain":
            return [("QUERY PLAN", t.TEXT)]
        if statement.kind != "query":
            return []
        text = "DESCRIBE " + sql.generate(statement.tree)

        def run() -> list[Any]:
            cursor = self.server.engine.cursor()
            try:
                return cursor.execute(text).fetchall()
            finally:
                cursor.close()

        rows = await asyncio.get_running_loop().run_in_executor(None, run)
        return [(row[0], t.oid_for(row[1])) for row in rows]

    def formats(self, portal: Portal) -> list[int] | None:
        if not portal.columns or not portal.result_formats:
            return None
        if len(portal.result_formats) == 1:
            return portal.result_formats * len(portal.columns)
        return portal.result_formats

    async def execute_message(self, body: bytes) -> None:
        name_end = body.index(b"\0")
        name = body[:name_end].decode()
        limit = struct.unpack("!i", body[name_end + 1 : name_end + 5])[0]
        portal = self.portals.get(name)
        if portal is None:
            raise sql.SqlError(f'portal "{name}" does not exist', "34000")
        await self.start(portal)
        await self.stream(portal, limit)

    def close_message(self, body: bytes) -> None:
        name = body[1:-1].decode()
        if body[:1] == b"S":
            self.prepared.pop(name, None)
        else:
            portal = self.portals.pop(name, None)
            if portal is not None and portal.cursor is not None:
                portal.cursor.close()
        self.send(w.message(b"3"))

    # execution ---------------------------------------------------------------------------

    async def start(self, portal: Portal) -> None:
        """Run the statement once; Describe and Execute both end up here."""
        if portal.cursor is not None or portal.canned is not None or portal.done:
            return
        statement = portal.prepared.statement
        kind = statement.kind
        if kind == "query" or kind == "explain":
            await self.run_query(portal, statement)
        elif kind == "show":
            portal.columns = [(statement.name, t.TEXT)]
            portal.canned = [(self.show(statement.name),)]
        elif kind == "set":
            await self.apply_setting(statement.name, statement.value)
            portal.done = True
        elif kind == "begin":
            self.tx = b"T"
            portal.done = True
        elif kind in {"commit", "rollback"}:
            self.tx = b"I"
            portal.done = True
        else:
            portal.done = True

    def show(self, name: str) -> str:
        if name == "all":
            raise sql.SqlError("SHOW ALL is not supported", "0A000")
        if name in self.settings:
            return self.settings[name]
        raise sql.SqlError(f'unrecognized configuration parameter "{name}"', "42704")

    async def apply_setting(self, name: str, value: str) -> None:
        self.settings[name] = value
        for key in STATUS_SETTINGS:
            if key.lower() == name and key not in {"server_version", "server_encoding", "is_superuser"}:
                self.send(w.parameter_status(key, value))

    async def run_query(self, portal: Portal, statement: sql.Statement) -> None:
        self.server.queries_total += 1
        text = sql.generate(statement.tree, portal.params)
        if statement.kind == "explain":
            text = "EXPLAIN " + text
        cursor = self.server.engine.cursor()
        zone = self.settings.get("timezone", "UTC")
        self.active = cursor
        log.debug("sql: %s", text)

        def run() -> None:
            if zone and zone.upper() != "UTC":
                cursor.execute(f"SET TimeZone = '{zone.replace(chr(39), '')}'")
            cursor.execute(text)

        try:
            await asyncio.get_running_loop().run_in_executor(None, run)
        except Exception:
            cursor.close()
            self.active = None
            raise
        if statement.kind == "explain":
            rows = cursor.fetchall()
            cursor.close()
            self.active = None
            portal.columns = [("QUERY PLAN", t.TEXT)]
            portal.canned = [(line,) for row in rows for line in str(row[-1]).splitlines()]
            return
        portal.cursor = cursor
        portal.columns = [(item[0], t.oid_for(item[1])) for item in cursor.description or []]

    async def stream(self, portal: Portal, limit: int) -> None:
        statement = portal.prepared.statement
        formats = self.formats(portal)
        if statement.kind == "empty":
            self.send(w.message(b"I"))
            return
        if portal.canned is not None:
            rows, portal.canned = portal.canned, []
            for row in rows:
                self.send(w.encode_row(row, portal.columns or [], formats))
            self.send(w.command_complete(f"{statement.tag} {len(rows)}" if statement.tag != "SHOW" else "SHOW"))
            return
        if portal.cursor is None:
            self.send(w.command_complete(statement.tag))
            return

        loop = asyncio.get_running_loop()
        columns = portal.columns or []
        sent_now = 0
        try:
            while True:
                if not portal.buffer:
                    size = FETCH_SIZE
                    portal.buffer = await loop.run_in_executor(None, portal.cursor.fetchmany, size)
                    if not portal.buffer:
                        break
                take = len(portal.buffer) if limit <= 0 else min(len(portal.buffer), limit - sent_now)
                chunk, portal.buffer = portal.buffer[:take], portal.buffer[take:]
                self.send(b"".join(w.encode_row(row, columns, formats) for row in chunk))
                sent_now += len(chunk)
                portal.sent += len(chunk)
                await self.flush()
                if limit > 0 and sent_now >= limit:
                    self.send(w.message(b"s"))
                    self.active = None
                    return
        except Exception:
            portal.cursor.close()
            portal.cursor = None
            self.active = None
            raise
        self.send(w.command_complete(f"SELECT {portal.sent}"))
        portal.cursor.close()
        portal.cursor = None
        portal.done = True
        self.active = None


async def serve_metrics(server: Server, host: str, port: int) -> asyncio.AbstractServer:
    """Plain HTTP: /healthz and Prometheus /metrics."""

    def metrics() -> str:
        lines = [
            f"pgfederate_up 1",
            f"pgfederate_connections_total {server.connections_total}",
            f"pgfederate_connections_active {len(server.sessions)}",
            f"pgfederate_queries_total {server.queries_total}",
            f"pgfederate_query_errors_total {server.errors_total}",
        ]
        for status in server.engine.status.values():
            labels = f'source="{status.name}",type="{status.type}"'
            lines.append(f"pgfederate_source_ok{{{labels}}} {int(status.status == 'ok')}")
            if status.last_refresh:
                lines.append(f"pgfederate_source_last_refresh_seconds{{{labels}}} {status.last_refresh:.0f}")
            if status.duration is not None:
                lines.append(f"pgfederate_source_refresh_duration_seconds{{{labels}}} {status.duration:.3f}")
            if status.rows is not None:
                lines.append(f"pgfederate_source_rows{{{labels}}} {status.rows}")
        return "\n".join(lines) + "\n"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), 5)
            path = request.split(b" ")[1].decode() if b" " in request else ""
            failing = [s.name for s in server.engine.status.values() if s.status != "ok"]
            if path == "/metrics":
                status, body = "200 OK", metrics()
            elif path == "/healthz":
                status = "503 Service Unavailable" if failing else "200 OK"
                body = f"failing: {', '.join(failing)}\n" if failing else "ok\n"
            else:
                status, body = "404 Not Found", "not found\n"
            data = body.encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(data)}\r\n"
                f"Connection: close\r\n\r\n".encode() + data
            )
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)


async def refresh_loop(engine: Engine) -> None:
    loop = asyncio.get_running_loop()

    async def one(source: Any) -> None:
        while True:
            await asyncio.sleep(source.refresh)
            await loop.run_in_executor(None, engine.refresh, source)

    await asyncio.gather(*(one(s) for s in engine.config.sources if s.refresh))


async def serve(config: Config, ready: asyncio.Event | None = None, engine: Engine | None = None) -> None:
    engine = engine or Engine(config)
    if engine.db is None:
        engine.start()
    server = Server(config, engine)
    listener = await asyncio.start_server(server.handle, config.host, config.port)
    port = listener.sockets[0].getsockname()[1]
    config.port = port
    metrics = None
    if config.metrics_port is not None:
        metrics = await serve_metrics(server, config.host, config.metrics_port)
        config.metrics_port = metrics.sockets[0].getsockname()[1]
    refresher = asyncio.create_task(refresh_loop(engine))
    if not config.users and config.host not in {"127.0.0.1", "localhost", "::1"}:
        log.warning("no users are configured: anyone who can reach %s:%s can read every source", config.host, port)
    log.info("listening on %s:%s, database %s", config.host, port, config.database)
    if ready is not None:
        ready.set()
    try:
        async with listener:
            await listener.serve_forever()
    finally:
        refresher.cancel()
        if metrics is not None:
            metrics.close()
        engine.close()
