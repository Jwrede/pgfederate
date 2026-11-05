# pgfederate

One read-only PostgreSQL endpoint over PostgreSQL, MySQL, SQLite, CSV/Parquet/JSON files and JSON REST APIs, described in one YAML file and executed by an embedded [DuckDB](https://duckdb.org).

A BI tool connects with its ordinary PostgreSQL driver and sees one database with one schema per source. It can then join tables from different systems, because the join runs inside the gateway. No plugin, custom driver or copy of the data into a warehouse is needed.

```
Metabase --+                             +-- PostgreSQL (live, filters pushed down)
Grafana  --+   PostgreSQL protocol       +-- MySQL (live, filters pushed down)
psql     --+-------> pgfederate ---------+-- SQLite (live)
JDBC     --+      (DuckDB in-process)    +-- CSV/Parquet/JSON files (live)
                                         +-- REST APIs (loaded on a schedule)
```

## Why

BI tools connect to one database at a time, and the requests to change this are among the most popular in their trackers. Figures from 5 November 2026:

| Request | Reactions | Open since |
|---|---|---|
| [metabase #3953](https://github.com/metabase/metabase/issues/3953) Join across multiple databases | 2110 | 2016 |
| [metabase #1300](https://github.com/metabase/metabase/issues/1300) Driver: ElasticSearch | 1509 | 2015 |
| [metabase #4831](https://github.com/metabase/metabase/issues/4831) Driver: REST API | 889 | 2017 |
| [metabase #7985](https://github.com/metabase/metabase/issues/7985) Driver: Google Sheets | 205 | 2018 |

The answers in those threads are [Trino or Presto](https://github.com/metabase/metabase/issues/3953), foreign data wrappers, the `dblink` extension, or [pgsql-http](https://github.com/pramsey/pgsql-http) inside your own PostgreSQL. Each of these works. Each also needs either a JVM cluster or a PostgreSQL server that you may change, plus per-source setup in SQL. Steampipe gives SQL access to SaaS APIs through per-service plugins.

Several projects put a PostgreSQL protocol in front of DuckDB. [duckgres](https://github.com/PostHog/duckgres) was discontinued by PostHog in October 2026, and [buenavista](https://github.com/jwills/buenavista) has not changed since December 2024. pgfederate is aimed at the small case those tools leave open: a single process that a team without a data platform can start next to Metabase.

## Install

```
pip install git+https://github.com/Jwrede/pgfederate
```

Python 3.11 or newer. DuckDB downloads its `postgres`, `mysql` and `sqlite` extensions on first use, or use the image, which contains them:

```
docker build -t pgfederate .
docker run -p 5433:5433 -v $PWD/pgfederate.yaml:/etc/pgfederate/pgfederate.yaml:ro pgfederate
```

## Example

`examples/quickstart` joins a SQLite database with a CSV file:

```yaml
server:
  port: 5433

sources:
  - name: shop
    type: sqlite
    path: shop.db

  - name: billing
    type: files
    tables:
      invoices: invoices.csv

relations:
  - from: billing.invoices.customer_id
    to: shop.customers.id
```

```console
$ cd examples/quickstart && python create_shop_db.py
$ pgfederate check -c pgfederate.yaml
shop (sqlite): ok, 1 tables
  shop.customers
billing (files): ok, 1 tables
  billing.invoices
$ pgfederate serve -c pgfederate.yaml &
$ psql -h 127.0.0.1 -p 5433 -U demo -d pgfederate
pgfederate=> select c.name, sum(i.amount) as billed
             from shop.customers c join billing.invoices i on i.customer_id = c.id
             group by c.name order by billed desc;
  name  | billed
--------+--------
 Acme   |   30.5
 Globex |   5.25
(2 rows)
```

`examples/compose` is a complete stack with PostgreSQL, MySQL, a CSV file, a JSON API, Metabase and Grafana. CI runs that stack, see [Testing](#testing).

## Connecting Metabase

Add a database of type **PostgreSQL** with the host and port of the gateway, database `pgfederate` and a user from the config. After the sync, every source appears as a schema. A `relations` entry is presented as a foreign key, so the query builder offers joins across sources without SQL.

For Grafana, add a PostgreSQL data source in the same way.

## Configuration

```yaml
server:
  host: 127.0.0.1          # default
  port: 5433               # default
  database: pgfederate     # name shown to clients
  metrics_port: 9187       # optional, serves /metrics and /healthz
  memory_limit: 2GB        # optional, passed to DuckDB
  tls: {cert: server.crt, key: server.key}

users:                     # without users, anyone who can connect may read
  - name: bi
    password: ${BI_PASSWORD}
  - name: finance
    password: ${FINANCE_PASSWORD}
    sources: [billing]     # optional, the user sees only these sources

sources:
  - name: crm              # becomes the schema name
    type: postgres
    dsn: postgresql://reader:${CRM_PASSWORD}@db:5432/crm
    schemas: [public]      # default for postgres
    include: ["*"]         # glob patterns on table names
    exclude: ["audit_*"]
    refresh: 5m            # how often to look for new tables

  - name: shop
    type: mysql
    dsn: host=db port=3306 user=reader password=${SHOP_PASSWORD} database=shop
    schemas: [shop]

  - name: legacy
    type: sqlite
    path: legacy.db

  - name: billing
    type: files
    tables:
      invoices: data/invoices/*.parquet          # read at query time
      rates:
        path: https://example.com/rates.csv      # downloaded, reloaded on refresh
        format: csv
    refresh: 1h

  - name: tracker
    type: rest
    base_url: https://api.github.com
    headers: {Authorization: "Bearer ${GITHUB_TOKEN}"}
    refresh: 15m
    tables:
      issues:
        path: /repos/metabase/metabase/issues
        params: {state: open, per_page: 100}
        pagination: {type: link}
        columns:                                 # optional: select and cast
          number: INTEGER
          title: VARCHAR
          created_at: TIMESTAMP

relations:                 # shown to clients as foreign keys
  - from: shop.orders.customer_id
    to: crm.customers.id

primary_keys:              # for sources that have none
  tracker.issues: number
```

`${VAR}` and `${VAR:-default}` read the environment. A PostgreSQL source with several schemas names its tables `schema__table`.

### REST tables

| Option | Meaning |
|---|---|
| `path`, `params`, `method`, `body` | The request. `body` is sent as JSON. |
| `records` | Dotted path to the list inside the response, such as `data.items`. Default: the response itself. |
| `pagination.type` | `link` (RFC 8288 header), `page`, `offset`, `cursor`, `next_url`, or `none`. |
| `pagination.param`, `limit`, `limit_param`, `start`, `path` | Details of the chosen type. |
| `pagination.max_pages` | Default 1000. Reaching it is an error, so a table is never silently cut off. |
| `columns` | Keep only these columns and cast them. Also gives an empty response a schema. |
| `flatten_depth` | Nested objects become `a__b` columns down to this depth (default 3). Lists are kept as JSON text. |

Requests are retried with backoff on 429, 5xx and network errors, and `Retry-After` is honoured. If a refresh fails, the previous data stays available and the error appears in `pgfederate.sources`.

## Operations

`select * from pgfederate.sources` shows each source's status, table count, last refresh and error, so a dashboard can show how fresh its data is.

With `metrics_port`, `/metrics` serves Prometheus metrics and `/healthz` returns 503 while a source is failing.

## Security model

The gateway is read-only on three levels:

1. **Statement allow-list.** Every statement is parsed. Only reads pass, and table references must point to an exposed source or the emulated catalog. A statement that cannot be parsed is refused.
2. **Function deny-list.** DuckDB functions that reach outside the exposed tables are refused, among them `read_*`, `glob`, `query`, `duckdb_*` and the scanner functions.
3. **Locked database.** After loading, DuckDB runs with `enable_external_access = false` and only the configured directories allowed. Databases are attached `READ_ONLY`.

Connection strings stay inside the process. Clients cannot read them, because `duckdb_databases()` and the attached catalogs are refused. Passwords use SCRAM-SHA-256. Use `tls` when the connection leaves the host, and give the gateway a read-only account on each source.

A logged-in user can still start an expensive query. There are no per-user quotas, so set `memory_limit` and keep the gateway private.

## How it works

Each client statement goes through [sqlglot](https://github.com/tobymao/sqlglot), which parses it as PostgreSQL. The rewriter applies the allow-list, moves `pg_catalog` and `information_schema` references to emulated tables, and replaces PostgreSQL-only functions. DuckDB executes the result and the rows go back in text or binary format over protocol version 3, including prepared statements, portals, cancel requests and SCRAM.

The emulated catalog is not DuckDB's own `pg_catalog`, which describes DuckDB. It is a set of ordinary tables rebuilt from the sources after every refresh, showing one database with one schema per source. Each restricted user gets a separate copy without the sources they may not read.

## Limits

- Read-only by design. Metabase actions and other writes do not work.
- It is not a query engine for large data. Everything runs in one process, and a cross-source join pulls the rows it needs from the sources into DuckDB. Filters are pushed down to PostgreSQL and MySQL. Aggregations are not.
- REST data is a snapshot from the last refresh, held in memory.
- `DECLARE`, `FETCH` and `LISTEN` are refused. Named cursors in psycopg therefore do not work. Portals, which JDBC uses, do.
- SQL that DuckDB cannot run fails, even if it is valid PostgreSQL. Generated BI queries are covered by the tests listed below, but hand-written SQL can use PostgreSQL features with no DuckDB equivalent, such as `LATERAL` forms or specific functions.
- Regular expressions run on RE2, which has no lookaround. Metabase's `host` and `domain` expressions use lookaround and therefore fail.
- The catalog covers what the tested clients ask for. Comments on tables and columns are not carried over. Foreign keys are read from PostgreSQL only; for other sources declare them under `relations`.
- `file` sources on S3 are not supported. Public `http(s)` files are.
- Metabase's own PostgreSQL connection options such as SSH tunnels are outside this project's tests.

## Testing

```
pip install -e .[dev]
pytest
```

The test suite runs real clients against a real server: psycopg (text and binary), asyncpg, and `psql` with its `\d` commands. It replays the catalog queries of Metabase, pgjdbc and Grafana, taken from their source code. `tests/test_security.py` tries about 35 escapes and writes. Set `PGFEDERATE_TEST_POSTGRES` and `PGFEDERATE_TEST_MYSQL` to include the database sources.

CI additionally starts `examples/compose` and drives real Metabase and Grafana through their HTTP APIs (`tests/e2e/run.py`): connection check, schema sync, primary and cross-source foreign keys, a native query across PostgreSQL, MySQL and REST, a matrix of about 80 query builder cases (aggregations, date buckets, filters, expressions, joins, nested queries), report time zones, field values and automatic dashboards.

## License

MIT
