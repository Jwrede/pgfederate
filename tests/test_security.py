"""The statement allow-list: a client must reach exposed tables and nothing else."""

import psycopg
import pytest


@pytest.mark.parametrize(
    "statement",
    [
        "insert into shop.customers values (9, 'x', 'y')",
        "update shop.customers set name = 'x'",
        "delete from shop.customers",
        "drop table shop.customers",
        "create table shop.t (a int)",
        "truncate shop.customers",
        "select * into shop.t from shop.customers",
        "copy shop.customers to '/tmp/x.csv'",
        "attach '/tmp/x.db' as x",
        "detach __src_shop",
        "install httpfs",
        "load httpfs",
        "pragma database_list",
        "call postgres_clear_cache()",
    ],
)
def test_writes_and_admin_statements_are_refused(dsn, statement):
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "select * from __src_shop.main.secrets",
        '''select * from "__src_shop"."main"."secrets"''',
        "select * from shop.secrets",
        "select * from __pg.pg_class",
        "select * from main.anything",
        "select * from read_csv('/etc/passwd')",
        "select * from read_text('/etc/passwd')",
        "select * from glob('/etc/*')",
        "select * from query('select 1')",
        "select * from query_table('shop.customers')",
        "select * from duckdb_databases()",
        "select * from duckdb_settings()",
        "select * from duckdb_secrets()",
        "select * from sniff_csv('/etc/passwd')",
        "select * from postgres_query('x', 'select 1')",
        "select (select count(*) from read_csv('/etc/passwd'))",
        "select 1 from shop.customers, lateral read_csv('/etc/passwd')",
        "select * from '/etc/passwd'",
        "with x as (select * from read_csv('/etc/passwd')) select * from x",
        "select getenv('HOME')",
    ],
)
def test_escapes_are_refused(dsn, statement):
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(statement)


def test_file_access_is_locked_at_database_level(server, data_dir):
    """Even a statement that passed the parser could not read other files."""
    import duckdb

    from pgfederate.engine import Engine

    from .conftest import make_config

    engine = Engine(make_config(data_dir, "http://127.0.0.1:1"))
    engine.start()
    try:
        cursor = engine.cursor()
        with pytest.raises(duckdb.PermissionException):
            cursor.execute(f"select * from read_csv('/etc/passwd')").fetchall()
        with pytest.raises(duckdb.Error):
            cursor.execute("SET enable_external_access = true")
    finally:
        engine.close()


def test_user_is_limited_to_their_sources(server):
    dsn = f"host=127.0.0.1 port={server.port} user=analyst password=pw dbname=pgfederate sslmode=disable"
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("select count(*) from billing.invoices").fetchone() == (3,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from shop.customers")
        with pytest.raises(psycopg.errors.UndefinedTable):
            conn.execute("select * from customers")


def test_restricted_user_cannot_read_source_status(server):
    dsn = f"host=127.0.0.1 port={server.port} user=analyst password=pw dbname=pgfederate sslmode=disable"
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from pgfederate.sources")


def test_oversized_login_message_closes_the_connection(server):
    import socket
    import struct

    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        payload = struct.pack("!I", 196608) + b"user\0admin\0\0"
        sock.sendall(struct.pack("!I", len(payload) + 4) + payload)
        assert sock.recv(1) == b"R"
        sock.recv(4096)
        sock.sendall(b"p" + struct.pack("!I", 2**31))
        assert sock.recv(4096) == b""


def test_user_catalog_hides_other_sources(server):
    dsn = f"host=127.0.0.1 port={server.port} user=analyst password=pw dbname=pgfederate sslmode=disable"
    with psycopg.connect(dsn, autocommit=True) as conn:
        schemas = {row[0] for row in conn.execute("select nspname from pg_namespace").fetchall()}
        assert "billing" in schemas and "shop" not in schemas
        tables = {row[0] for row in conn.execute("select table_name from information_schema.tables").fetchall()}
        assert "invoices" in tables and "customers" not in tables
        keys = conn.execute("select conname from pg_constraint where contype = 'f'").fetchall()
        assert keys == []  # the relation points into a hidden source


def test_excluded_table_is_absent_from_catalog(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        names = {row[0] for row in conn.execute("select relname from pg_class").fetchall()}
    assert "customers" in names and "secrets" not in names
