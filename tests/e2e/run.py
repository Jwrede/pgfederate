#!/usr/bin/env python3
"""End-to-end check with real Metabase and Grafana behind their PostgreSQL drivers.

Expects `docker compose up` from examples/compose. Standard library only.
"""

import json
import sys
import time
import urllib.error
import urllib.request

METABASE = "http://127.0.0.1:3000"
GRAFANA = "http://127.0.0.1:3001"
DEADLINE = 420


def call(base, path, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"{path}: HTTP {error.code}: {error.read().decode()[:2000]}")
    return json.loads(text) if text else None


def wait_for(description, check, limit=DEADLINE):
    deadline = time.time() + limit
    last = None
    while time.time() < deadline:
        try:
            result = check()
            if result:
                print(f"ok: {description}")
                return result
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError, SystemExit) as error:
            last = error
        time.sleep(3)
    raise SystemExit(f"timed out: {description} ({last})")


def metabase():
    wait_for("metabase is up", lambda: call(METABASE, "/api/health").get("status") == "ok")
    token = call(METABASE, "/api/session/properties")["setup-token"]
    session = call(METABASE, "/api/setup", {
        "token": token,
        "user": {"email": "e2e@example.com", "password": "Passw0rd-e2e-test!", "first_name": "E", "last_name": "E",
                 "site_name": "e2e"},
        "prefs": {"site_name": "e2e", "site_locale": "en", "allow_tracking": False},
    })["id"]
    auth = {"X-Metabase-Session": session}
    database = call(METABASE, "/api/database", {
        "engine": "postgres", "name": "pgfederate",
        "details": {"host": "gateway", "port": 5433, "dbname": "pgfederate", "user": "bi",
                    "password": "bi-password", "ssl": False},
    }, auth)["id"]
    print(f"ok: metabase accepted the connection (database {database})")
    call(METABASE, f"/api/database/{database}/sync_schema", {}, auth)

    expected = {("crm", "customers"), ("crm", "regions"), ("shop", "orders"), ("shop", "products"),
                ("finance", "budgets"), ("tickets", "tickets")}

    def synced():
        meta = call(METABASE, f"/api/database/{database}/metadata", headers=auth)
        found = {(t["schema"], t["name"]) for t in meta["tables"]}
        return meta if expected <= found and all(t["fields"] for t in meta["tables"]) else None

    meta = wait_for("metabase synced every source", synced)
    tables = {(t["schema"], t["name"]): t for t in meta["tables"]}
    fields = {(t["schema"], t["name"], f["name"]): f for t in meta["tables"] for f in t["fields"]}

    assert fields[("crm", "customers", "id")]["semantic_type"] == "type/PK", "primary key of a Postgres table"
    assert fields[("tickets", "tickets", "id")]["semantic_type"] == "type/PK", "declared primary key"
    assert fields[("crm", "customers", "signed_up")]["base_type"] == "type/DateTimeWithLocalTZ"

    def foreign_keys():
        current = call(METABASE, f"/api/database/{database}/metadata", headers=auth)
        by = {(t["schema"], t["name"], f["name"]): f for t in current["tables"] for f in t["fields"]}
        return by if by[("shop", "orders", "customer_id")]["fk_target_field_id"] else None

    fields = wait_for("metabase sees the cross-source foreign key", foreign_keys, 120)
    target = fields[("shop", "orders", "customer_id")]["fk_target_field_id"]
    assert target == fields[("crm", "customers", "id")]["id"]

    native = call(METABASE, "/api/dataset", {
        "database": database, "type": "native",
        "native": {"query": """
            SELECT c.name, sum(o.quantity * p.price) AS revenue, coalesce(max(t.n), 0) AS tickets
            FROM crm.customers c
            JOIN shop.orders o ON o.customer_id = c.id
            JOIN shop.products p ON p.id = o.product_id
            LEFT JOIN (SELECT customer_id, count(*) AS n FROM tickets.tickets GROUP BY customer_id) t
              ON t.customer_id = c.id
            GROUP BY c.name ORDER BY c.name"""},
    }, auth)
    assert not native.get("error"), native.get("error")
    rows = native["data"]["rows"]
    assert [(row[0], float(row[1]), row[2]) for row in rows] == [
        ("Acme", 53.5, 2), ("Globex", 50.0, 1), ("Initech", 9.5, 0),
    ], rows
    print("ok: native query joins Postgres, MySQL and REST:", rows)

    amount = fields[("shop", "orders", "quantity")]["id"]
    name = fields[("crm", "customers", "name")]["id"]
    customer = fields[("shop", "orders", "customer_id")]["id"]
    ordered = fields[("shop", "orders", "ordered_at")]["id"]
    structured = call(METABASE, "/api/dataset", {
        "database": database, "type": "query",
        "query": {
            "source-table": tables[("shop", "orders")]["id"],
            "aggregation": [["sum", ["field", amount, None]]],
            "breakout": [["field", name, {"source-field": customer}]],
        },
    }, auth)
    assert not structured.get("error"), structured.get("error")
    result = {row[0]: row[1] for row in structured["data"]["rows"]}
    assert result == {"Acme": 4, "Globex": 2, "Initech": 1}, result
    print("ok: query builder join across MySQL and Postgres:", result)

    temporal = call(METABASE, "/api/dataset", {
        "database": database, "type": "query",
        "query": {
            "source-table": tables[("shop", "orders")]["id"],
            "aggregation": [["count"]],
            "breakout": [["field", ordered, {"temporal-unit": "month"}]],
        },
    }, auth)
    assert not temporal.get("error"), temporal.get("error")
    assert [row[1] for row in temporal["data"]["rows"]] == [2, 2], temporal["data"]["rows"]
    print("ok: month bucketing:", temporal["data"]["rows"])

    filtered = call(METABASE, "/api/dataset", {
        "database": database, "type": "query",
        "query": {
            "source-table": tables[("crm", "customers")]["id"],
            "filter": ["=", ["field", fields[("crm", "customers", "name")]["id"], None], "Acme"],
        },
    }, auth)
    assert not filtered.get("error"), filtered.get("error")
    assert len(filtered["data"]["rows"]) == 1
    print("ok: filter with a bound parameter")


def grafana():
    basic = "Basic " + __import__("base64").b64encode(b"admin:admin").decode()
    auth = {"Authorization": basic}
    wait_for("grafana is up", lambda: call(GRAFANA, "/api/health").get("database") == "ok")
    source = call(GRAFANA, "/api/datasources", {
        "name": "pgfederate", "type": "postgres", "access": "proxy", "url": "gateway:5433", "user": "bi",
        "database": "pgfederate", "secureJsonData": {"password": "bi-password"},
        "jsonData": {"database": "pgfederate", "sslmode": "disable", "postgresVersion": 1500},
    }, auth)["datasource"]
    uid = source["uid"]
    health = call(GRAFANA, f"/api/datasources/uid/{uid}/health", headers=auth)
    assert health["status"] == "OK", health
    print("ok: grafana health check:", health["message"])
    answer = call(GRAFANA, "/api/ds/query", {
        "from": "now-1h", "to": "now",
        "queries": [{
            "refId": "A", "datasource": {"uid": uid, "type": "postgres"}, "format": "table",
            "rawSql": """SELECT c.name, count(*) AS orders FROM shop.orders o
                         JOIN crm.customers c ON c.id = o.customer_id GROUP BY c.name ORDER BY c.name""",
        }],
    }, auth)
    frame = answer["results"]["A"]["frames"][0]
    assert frame["data"]["values"][0] == ["Acme", "Globex", "Initech"], frame
    assert frame["data"]["values"][1] == [2, 1, 1], frame
    print("ok: grafana cross-source query")


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    if only in {"all", "grafana"}:
        grafana()
    if only in {"all", "metabase"}:
        metabase()
    print("end-to-end checks passed")
