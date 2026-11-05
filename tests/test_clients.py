import asyncio
import datetime as dt
import decimal
import subprocess

import asyncpg
import psycopg
import pytest


def fetch(dsn, sql, params=None, **kwargs):
    with psycopg.connect(dsn, **kwargs) as conn:
        return conn.execute(sql, params).fetchall()


def test_simple_select(dsn):
    assert fetch(dsn, "select 1 + 1 as two") == [(2,)]


def test_sqlite_table(dsn):
    assert fetch(dsn, "select name from shop.customers order by id")[0] == ("Acme",)


def test_cross_source_join(dsn):
    rows = fetch(
        dsn,
        """select c.name, sum(i.amount) as total
           from shop.customers c join billing.invoices i on i.customer_id = c.id
           group by c.name order by c.name""",
    )
    assert [(name, float(total)) for name, total in rows] == [("Acme", 30.5), ("Globex", 5.25)]


def test_rest_table_flattened_and_paginated(dsn):
    rows = fetch(dsn, "select id, user__login, labels from tracker.issues order by id")
    assert len(rows) == 7
    assert rows[0] == (1, "alice", '["bug"]')


def test_rest_records_path(dsn):
    assert fetch(dsn, "select count(*) from tracker.wrapped") == [(3,)]


def test_file_from_url(dsn):
    assert fetch(dsn, "select rate from billing.rates where code = 'USD'") == [(1.1,)]


def test_parameters(dsn):
    assert fetch(dsn, "select name from shop.customers where id = %s", (2,)) == [("Globex",)]
    assert fetch(dsn, "select name from shop.customers where country = %s order by id", ("DE",))[1] == ("Initech",)


def test_bare_table_name_is_resolved(dsn):
    assert fetch(dsn, "select count(*) from customers") == [(3,)]


def test_types_in_text_format(dsn):
    row = fetch(
        dsn,
        """select true, 1::smallint, 2::bigint, 1.5::double precision, 12.50::numeric(10,2), 'x'::text,
                  date '2026-01-02', timestamp '2026-01-02 03:04:05', '\\x0102'::blob""",
    )[0]
    assert row[:6] == (True, 1, 2, 1.5, decimal.Decimal("12.50"), "x")
    assert row[6] == dt.date(2026, 1, 2)
    assert row[7] == dt.datetime(2026, 1, 2, 3, 4, 5)


def test_psycopg_binary_results(dsn):
    with psycopg.connect(dsn) as conn:
        with conn.cursor(binary=True) as cursor:
            cursor.execute(
                """select true, 7::integer, 8::bigint, 2.5::double precision, 123.45::decimal(10,2),
                          date '2026-03-04', timestamp '2026-03-04 05:06:07', 'text'::varchar"""
            )
            assert cursor.fetchone() == (
                True, 7, 8, 2.5, decimal.Decimal("123.45"), dt.date(2026, 3, 4),
                dt.datetime(2026, 3, 4, 5, 6, 7), "text",
            )


def test_asyncpg(server):
    async def run():
        conn = await asyncpg.connect(
            host="127.0.0.1", port=server.port, user="admin", password="s3cret", database="pgfederate", ssl=False
        )
        try:
            rows = await conn.fetch("select id, name from shop.customers where id = $1::int", 3)
            assert rows[0]["name"] == "Initech"
            value = await conn.fetchval("select sum(amount) from billing.invoices")
            assert float(value) == 35.75
            stamp = await conn.fetchval("select timestamp '2026-01-02 03:04:05'")
            assert stamp == dt.datetime(2026, 1, 2, 3, 4, 5)
        finally:
            await conn.close()

    asyncio.run(run())


def test_column_names_follow_postgres(dsn):
    with psycopg.connect(dsn) as conn:
        cursor = conn.execute("select count(*), sum(id), current_database(), id::text as x from shop.customers group by id")
        assert [column.name for column in cursor.description] == ["count", "sum", "current_database", "x"]


def test_substring_from_pattern_is_a_regex_search(dsn):
    assert fetch(dsn, "select substring('Acme Corp' from '[A-Z]')") == [("A",)]
    assert fetch(dsn, "select substring('id=42;' from 'id=([0-9]+)')") == [("42",)]
    assert fetch(dsn, "select substring('Acme' from 2 for 2)") == [("cm",)]
    assert fetch(dsn, "select substring(name from %s) from shop.customers where id = 1", ("[A-Z]",)) == [("A",)]


def test_unquoted_identifiers_fold_case(dsn):
    assert fetch(dsn, "select count(*) from SHOP.CUSTOMERS") == [(3,)]


def test_named_cursors_are_refused_clearly(dsn):
    with psycopg.connect(dsn) as conn:
        with conn.cursor(name="c") as cursor:
            with pytest.raises(psycopg.errors.FeatureNotSupported):
                cursor.execute("select 1")


def test_portal_is_fetched_in_batches(server):
    async def run():
        conn = await asyncpg.connect(
            host="127.0.0.1", port=server.port, user="admin", password="s3cret", database="pgfederate", ssl=False
        )
        try:
            async with conn.transaction():
                cursor = await conn.cursor("select * from generate_series(1, 7) as g(n) order by n")
                first = await cursor.fetch(3)
                second = await cursor.fetch(3)
                third = await cursor.fetch(3)
            assert [r[0] for r in first + second + third] == [1, 2, 3, 4, 5, 6, 7]
        finally:
            await conn.close()

    asyncio.run(run())


def test_prepared_statement_reuse(server):
    async def run():
        conn = await asyncpg.connect(
            host="127.0.0.1", port=server.port, user="admin", password="s3cret", database="pgfederate", ssl=False
        )
        try:
            statement = await conn.prepare("select name from shop.customers where id = $1::bigint")
            assert await statement.fetchval(1) == "Acme"
            assert await statement.fetchval(2) == "Globex"
        finally:
            await conn.close()

    asyncio.run(run())


def test_error_inside_extended_protocol_recovers(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UndefinedTable):
            conn.execute("select * from shop.missing")
        assert conn.execute("select 1").fetchone() == (1,)


def test_failed_transaction_state(dsn):
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute("select * from shop.missing")
        conn.rollback()
        assert conn.execute("select 2").fetchone() == (2,)


def test_cancel_running_query(dsn):
    import threading
    import time

    with psycopg.connect(dsn, autocommit=True) as conn:
        def cancel():
            time.sleep(0.4)
            conn.cancel_safe(timeout=5)  # plain cancel() holds the GIL, and the server runs in this process

        thread = threading.Thread(target=cancel)
        thread.start()
        started = time.time()
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("select count(*) from range(100000000000) a, range(1000) b")
        thread.join()
        assert time.time() - started < 15
        assert conn.execute("select 3").fetchone() == (3,)


def test_many_rows_stream(dsn):
    assert fetch(dsn, "select count(*) from (select * from generate_series(1, 200000) as g(n)) x") == [(200000,)]
    assert len(fetch(dsn, "select * from generate_series(1, 50000) as g(n)")) == 50000


def test_multiple_statements_in_simple_protocol(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("select 1; select 2")
        assert cursor.fetchone() == (1,)
        assert cursor.nextset()
        assert cursor.fetchone() == (2,)


def test_time_zone_setting_applies(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET TIME ZONE 'Asia/Tokyo'")
        assert conn.execute("show timezone").fetchone() == ("Asia/Tokyo",)
        value = conn.execute("select cast(timestamptz '2026-01-01 00:00:00+00' as timestamp)").fetchone()[0]
        assert value == dt.datetime(2026, 1, 1, 9, 0)


def test_wrong_password(server):
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        psycopg.connect(f"host=127.0.0.1 port={server.port} user=admin password=wrong dbname=x sslmode=disable")


def test_unknown_user(server):
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        psycopg.connect(f"host=127.0.0.1 port={server.port} user=nobody password=x dbname=x sslmode=disable")


def test_psql_meta_commands(server):
    env = {"PGPASSWORD": "s3cret", "PATH": "/usr/bin:/bin"}
    base = ["psql", "-h", "127.0.0.1", "-p", str(server.port), "-U", "admin", "-d", "pgfederate", "-X", "-A", "-t"]
    tables = subprocess.run(base + ["-c", r"\dt shop.*"], env=env, capture_output=True, text=True, timeout=20)
    assert tables.returncode == 0, tables.stderr
    assert "customers" in tables.stdout
    detail = subprocess.run(base + ["-c", r"\d shop.customers"], env=env, capture_output=True, text=True, timeout=20)
    assert detail.returncode == 0, detail.stderr
    assert "name" in detail.stdout
    schemas = subprocess.run(base + ["-c", r"\dn"], env=env, capture_output=True, text=True, timeout=20)
    assert "billing" in schemas.stdout
