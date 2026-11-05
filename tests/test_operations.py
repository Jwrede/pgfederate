"""Refresh behaviour, metrics, TLS and the command line."""

import asyncio
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import psycopg
import pytest

from pgfederate import cli
from pgfederate import config as cfg
from pgfederate.engine import Engine
from pgfederate.server import serve

from .conftest import Api, make_config


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_sources_table_reports_status(dsn):
    with psycopg.connect(dsn) as conn:
        rows = dict(conn.execute("select name, status from pgfederate.sources").fetchall())
    assert rows == {"shop": "ok", "billing": "ok", "tracker": "ok"}


def test_metrics_and_health(server, dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute("select 1")
    status, body = get(f"http://127.0.0.1:{server.metrics_port}/metrics")
    assert status == 200
    assert 'pgfederate_source_ok{source="tracker",type="rest"} 1' in body
    assert "pgfederate_queries_total" in body
    assert get(f"http://127.0.0.1:{server.metrics_port}/healthz") == (200, "ok\n")
    assert get(f"http://127.0.0.1:{server.metrics_port}/nope")[0] == 404


def test_failed_refresh_keeps_last_good_data(data_dir, api):
    engine = Engine(make_config(data_dir, api))
    engine.start()
    try:
        source = next(s for s in engine.config.sources if s.name == "tracker")
        before = engine.cursor().execute("select count(*) from tracker.issues").fetchone()
        Api.fail_next = 100
        source.options["tables"]["issues"]["timeout"] = 1
        import pgfederate.rest as rest

        original, rest.MAX_RETRIES = rest.MAX_RETRIES, 0
        try:
            engine.refresh(source)
        finally:
            rest.MAX_RETRIES = original
            Api.fail_next = 0
        assert engine.status["tracker"].status == "error"
        assert engine.cursor().execute("select count(*) from tracker.issues").fetchone() == before
        row = engine.cursor().execute("select status, error from pgfederate.sources where name = 'tracker'").fetchone()
        assert row[0] == "error" and "503" in row[1]
    finally:
        engine.close()


def test_refresh_picks_up_new_columns(data_dir, api, tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("a\n1\n")
    config = cfg.parse(
        {"sources": [{"name": "f", "type": "files", "tables": {"t": {"path": str(path)}}}]}, tmp_path
    )
    engine = Engine(config)
    engine.start()
    try:
        path.write_text("a,b\n1,2\n")
        engine.refresh(config.sources[0])
        columns = engine.cursor().execute(
            "select attname from __pg.pg_attribute a join __pg.pg_class c on c.oid = a.attrelid where c.relname = 't'"
        ).fetchall()
        assert {row[0] for row in columns} == {"a", "b"}
    finally:
        engine.close()


def test_sqlite_refresh_sees_new_tables(tmp_path):
    import sqlite3

    path = tmp_path / "x.db"
    db = sqlite3.connect(path)
    db.execute("create table one (id integer primary key)")
    db.commit()
    config = cfg.parse({"sources": [{"name": "s", "type": "sqlite", "path": str(path)}]}, tmp_path)
    engine = Engine(config)
    engine.start()
    try:
        assert set(engine.exposed["s"]) == {"one"}
        db.execute("create table two (id integer primary key)")
        db.commit()
        engine.refresh(config.sources[0])
        assert engine.status["s"].status == "ok", engine.status["s"].error
        assert set(engine.exposed["s"]) == {"one", "two"}
    finally:
        engine.close()
        db.close()


def run_server(config):
    started = threading.Event()
    box = {}

    def target():
        loop = asyncio.new_event_loop()

        async def main():
            ready = asyncio.Event()
            box["loop"] = loop
            box["task"] = asyncio.create_task(serve(config, ready))
            await ready.wait()
            started.set()
            try:
                await box["task"]
            except asyncio.CancelledError:
                pass

        loop.run_until_complete(main())

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    assert started.wait(30)
    return box, thread


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_tls(data_dir, api, tmp_path):
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
         "-days", "1", "-subj", "/CN=localhost"],
        check=True, capture_output=True, timeout=30,
    )
    config = make_config(data_dir, api, tls={"cert": str(cert), "key": str(key)})
    box, thread = run_server(config)
    try:
        dsn = f"host=127.0.0.1 port={config.port} user=admin password=s3cret dbname=pgfederate sslmode=require"
        with psycopg.connect(dsn) as conn:
            assert conn.execute("select 1").fetchone() == (1,)
            assert conn.pgconn.ssl_in_use
    finally:
        box["loop"].call_soon_threadsafe(box["task"].cancel)
        thread.join(10)


def test_trust_mode_without_users(tmp_path):
    (tmp_path / "t.csv").write_text("a\n1\n")
    config = cfg.parse(
        {"server": {"port": 0}, "sources": [{"name": "f", "type": "files", "tables": {"t": "t.csv"}}]}, tmp_path
    )
    box, thread = run_server(config)
    try:
        with psycopg.connect(f"host=127.0.0.1 port={config.port} user=anyone dbname=x sslmode=disable") as conn:
            assert conn.execute("select a from f.t").fetchone() == (1,)
            assert conn.execute("select current_user").fetchone() == ("anyone",)
    finally:
        box["loop"].call_soon_threadsafe(box["task"].cancel)
        thread.join(10)


def test_cli_check(tmp_path, capsys):
    (tmp_path / "t.csv").write_text("a\n1\n")
    path = tmp_path / "c.yaml"
    path.write_text("sources:\n  - {name: f, type: files, tables: {t: t.csv}}\n")
    assert cli.main(["check", "-c", str(path)]) == 0
    out = capsys.readouterr().out
    assert "f (files): ok, 1 tables" in out and "f.t" in out


def test_cli_reports_config_errors(tmp_path, capsys):
    path = tmp_path / "c.yaml"
    path.write_text("sources: []\n")
    assert cli.main(["check", "-c", str(path)]) == 2
    assert "at least one source" in capsys.readouterr().err


def test_cli_check_fails_on_broken_source(tmp_path, capsys):
    path = tmp_path / "c.yaml"
    path.write_text("sources:\n  - {name: f, type: files, tables: {t: missing.csv}}\n")
    assert cli.main(["check", "-c", str(path)]) == 1
