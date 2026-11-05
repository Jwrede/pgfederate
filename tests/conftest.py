import asyncio
import csv
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pgfederate import config as cfg
from pgfederate.server import serve

ISSUES = [
    {"id": i, "title": f"issue {i}", "user": {"login": "alice" if i % 2 else "bob"}, "labels": ["bug"],
     "created_at": f"2026-01-{i:02d}T10:00:00Z"}
    for i in range(1, 8)
]


class Api(BaseHTTPRequestHandler):
    requests = []
    fail_next = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        from urllib.parse import parse_qs, urlsplit

        Api.requests.append(self.path)
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if Api.fail_next:
            Api.fail_next -= 1
            self.send_response(503)
            self.end_headers()
            return
        if parts.path == "/rates.csv":
            data = b"code,rate\nEUR,1.0\nUSD,1.1\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        page = int(query.get("page", ["1"])[0])
        size = 3
        items = ISSUES[(page - 1) * size : page * size]
        headers = {}
        if parts.path == "/issues":
            body = items
            if page * size < len(ISSUES):
                headers["Link"] = f'<http://127.0.0.1:{self.server.server_port}/issues?page={page + 1}>; rel="next"'
        elif parts.path == "/wrapped":
            body = {"data": {"items": items}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="session")
def api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("data")
    db = sqlite3.connect(root / "shop.db")
    db.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT);
        INSERT INTO customers VALUES (1, 'Acme', 'DE'), (2, 'Globex', 'US'), (3, 'Initech', 'DE');
        CREATE TABLE secrets (id INTEGER PRIMARY KEY, token TEXT);
        INSERT INTO secrets VALUES (1, 'hidden');
        """
    )
    db.close()
    with (root / "invoices.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "customer_id", "amount", "issued"])
        writer.writerows([[1, 1, "10.50", "2026-01-05"], [2, 1, "20.00", "2026-02-01"], [3, 2, "5.25", "2026-02-11"]])
    (root / "outside.csv").write_text("a\n1\n")
    return root


def make_config(data_dir, api, **server):
    return cfg.parse(
        {
            "server": {"port": 0, "metrics_port": 0, **server},
            "users": [
                {"name": "admin", "password": "s3cret"},
                {"name": "analyst", "password": "pw", "sources": ["billing"]},
            ],
            "sources": [
                {"name": "shop", "type": "sqlite", "path": str(data_dir / "shop.db"), "exclude": ["secrets"]},
                {
                    "name": "billing",
                    "type": "files",
                    "tables": {
                        "invoices": str(data_dir / "invoices.csv"),
                        "rates": f"{api}/rates.csv",
                    },
                },
                {
                    "name": "tracker",
                    "type": "rest",
                    "base_url": api,
                    "tables": {
                        "issues": {"path": "/issues", "pagination": {"type": "link"}},
                        "wrapped": {"path": "/wrapped", "records": "data.items"},
                    },
                },
            ],
            "relations": [{"from": "billing.invoices.customer_id", "to": "shop.customers.id"}],
        },
        data_dir,
    )


@pytest.fixture(scope="session")
def server(data_dir, api):
    config = make_config(data_dir, api)
    started = threading.Event()
    box = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ready = asyncio.Event()

        async def main():
            task = asyncio.create_task(serve(config, ready))
            box["loop"], box["task"] = loop, task
            waiter = asyncio.create_task(ready.wait())
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            started.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                box["error"] = error

        loop.run_until_complete(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(120), "server did not start"
    if not config.port:
        thread.join(5)
        raise RuntimeError(f"server failed: {box.get('error')}")
    yield config
    box["loop"].call_soon_threadsafe(box["task"].cancel)
    thread.join(10)


@pytest.fixture
def dsn(server):
    return f"host=127.0.0.1 port={server.port} user=admin password=s3cret dbname=pgfederate sslmode=disable"
