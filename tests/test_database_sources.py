"""PostgreSQL and MySQL sources. CI and the compose setup provide the servers."""

import os

import psycopg
import pytest

from pgfederate import config as cfg
from pgfederate.engine import Engine

POSTGRES = os.environ.get("PGFEDERATE_TEST_POSTGRES")
MYSQL = os.environ.get("PGFEDERATE_TEST_MYSQL")


@pytest.fixture
def postgres():
    if not POSTGRES:
        pytest.skip("PGFEDERATE_TEST_POSTGRES is not set")
    with psycopg.connect(POSTGRES, autocommit=True) as conn:
        conn.execute("drop schema if exists pgf_test cascade")
        conn.execute("create schema pgf_test")
        conn.execute("create table pgf_test.parents (id int primary key, name text not null)")
        conn.execute(
            "create table pgf_test.children (id int primary key, parent_id int references pgf_test.parents(id), n numeric(10,2))"
        )
        conn.execute("insert into pgf_test.parents values (1, 'a'), (2, 'b')")
        conn.execute("insert into pgf_test.children values (1, 1, 2.50), (2, 1, 3.50), (3, 2, 1.00)")
        yield conn
        conn.execute("drop schema pgf_test cascade")


def load(tmp_path, source):
    engine = Engine(cfg.parse({"sources": [source]}, tmp_path))
    engine.start()
    return engine


def test_postgres_tables_primary_and_foreign_keys(postgres, tmp_path):
    engine = load(tmp_path, {"name": "pg", "type": "postgres", "dsn": POSTGRES, "schemas": ["pgf_test"]})
    try:
        cursor = engine.cursor()
        assert set(engine.exposed["pg"]) == {"parents", "children"}
        rows = cursor.execute(
            "select p.name, sum(c.n) from pg.children c join pg.parents p on p.id = c.parent_id group by 1 order by 1"
        ).fetchall()
        assert [(name, float(total)) for name, total in rows] == [("a", 6.0), ("b", 1.0)]
        model = engine.model()
        tables = {table.name: table for table in model.tables}
        assert tables["parents"].primary_key == ["id"]
        key = next(k for k in model.foreign_keys if k.table == "children")
        assert (key.column, key.ref_table, key.ref_column) == ("parent_id", "parents", "id")
    finally:
        engine.close()


def test_postgres_filter_pushdown_reads_live_data(postgres, tmp_path):
    engine = load(tmp_path, {"name": "pg", "type": "postgres", "dsn": POSTGRES, "schemas": ["pgf_test"]})
    try:
        postgres.execute("insert into pgf_test.parents values (3, 'c')")
        assert engine.cursor().execute("select count(*) from pg.parents").fetchone() == (3,)
    finally:
        engine.close()


def test_postgres_filter_is_pushed_into_the_scan(postgres, tmp_path):
    engine = load(tmp_path, {"name": "pg", "type": "postgres", "dsn": POSTGRES, "schemas": ["pgf_test"]})
    try:
        plan = engine.cursor().execute("explain select * from pg.parents where id = 1").fetchall()[0][1]
        assert "POSTGRES_SCAN" in plan and "FILTER" not in plan, plan
    finally:
        engine.close()


def test_postgres_refresh_finds_new_tables(postgres, tmp_path):
    engine = load(tmp_path, {"name": "pg", "type": "postgres", "dsn": POSTGRES, "schemas": ["pgf_test"]})
    try:
        postgres.execute("create table pgf_test.later (id int)")
        engine.refresh(engine.config.sources[0])
        assert engine.status["pg"].status == "ok", engine.status["pg"].error
        assert "later" in engine.exposed["pg"]
    finally:
        engine.close()


def test_postgres_several_schemas_get_prefixes(postgres, tmp_path):
    postgres.execute("drop schema if exists pgf_other cascade")
    postgres.execute("create schema pgf_other")
    postgres.execute("create table pgf_other.parents (id int)")
    try:
        engine = load(
            tmp_path, {"name": "pg", "type": "postgres", "dsn": POSTGRES, "schemas": ["pgf_test", "pgf_other"]}
        )
        try:
            assert {"pgf_test__parents", "pgf_other__parents"} <= set(engine.exposed["pg"])
        finally:
            engine.close()
    finally:
        postgres.execute("drop schema pgf_other cascade")


def test_postgres_attach_is_read_only(postgres, tmp_path):
    import duckdb

    engine = load(tmp_path, {"name": "pg", "type": "postgres", "dsn": POSTGRES, "schemas": ["pgf_test"]})
    try:
        with pytest.raises(duckdb.Error):
            engine.cursor().execute('insert into "__src_pg".pgf_test.parents values (9, \'x\')')
    finally:
        engine.close()


def test_mysql_source(tmp_path):
    if not MYSQL:
        pytest.skip("PGFEDERATE_TEST_MYSQL is not set")
    engine = load(tmp_path, {"name": "my", "type": "mysql", "dsn": MYSQL, "schemas": ["shop"]})
    try:
        assert "products" in engine.exposed["my"]
        assert engine.cursor().execute("select count(*) from my.products").fetchone()[0] > 0
        assert engine.status["my"].status == "ok"
        plan = engine.cursor().execute("explain select * from my.products where id = 1").fetchall()[0][1]
        assert "FILTER" not in plan, plan
    finally:
        engine.close()
