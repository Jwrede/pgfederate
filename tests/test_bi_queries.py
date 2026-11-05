"""The catalog queries BI tools and drivers really send.

The Metabase queries are rendered from src/metabase/driver/postgres.clj. The JDBC
queries come from pgjdbc's TypeInfoCache and PgDatabaseMetaData. Grafana's come
from its PostgreSQL data source query builder.
"""

import psycopg
import pytest

METABASE_TABLES = """
SELECT n.nspname AS schema, c.relname AS name,
  CASE c.relkind WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'PARTITIONED TABLE' WHEN 'v' THEN 'VIEW'
                 WHEN 'f' THEN 'FOREIGN TABLE' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE NULL END AS type,
  d.description AS description, NULLIF(stat.n_live_tup, 0) AS estimated_row_count
FROM pg_catalog.pg_class AS c
INNER JOIN pg_catalog.pg_namespace AS n ON c.relnamespace = n.oid
LEFT JOIN pg_catalog.pg_description AS d ON (c.oid = d.objoid) AND (d.objsubid = 0) AND (d.classoid = 'pg_class'::regclass)
LEFT JOIN pg_stat_user_tables AS stat ON (n.nspname = stat.schemaname) AND (c.relname = stat.relname)
WHERE (c.relnamespace = n.oid) AND (n.nspname !~ '^pg_') AND (n.nspname <> 'information_schema')
  AND c.relkind in ('r', 'p', 'v', 'f', 'm') AND n.nspname IN ('shop', 'billing')
ORDER BY type ASC, schema ASC, name ASC
"""

METABASE_FIELDS = """
SELECT * FROM (
SELECT c.column_name AS name,
  CASE WHEN c.udt_schema IN ('public', 'pg_catalog') THEN format('%s', c.udt_name)
       ELSE format('"%s"."%s"', c.udt_schema, c.udt_name) END AS "database-type",
  c.ordinal_position - 1 AS "database-position", c.table_schema AS "table-schema",
  c.table_name AS "table-name", pk.column_name IS NOT NULL AS "pk?",
  col_description(CAST(CAST(format('%I.%I', CAST(c.table_schema AS TEXT), CAST(c.table_name AS TEXT)) AS REGCLASS) AS OID), c.ordinal_position) AS "field-comment",
  ((column_default IS NULL) OR (LOWER(column_default) = 'null')) AND (is_nullable = 'NO')
    AND NOT (((column_default IS NOT NULL) AND (column_default LIKE '%nextval(%')) OR (is_identity <> 'NO')) AS "database-required",
  column_default AS "database-default",
  ((column_default IS NOT NULL) AND (column_default LIKE '%nextval(%')) OR (is_identity <> 'NO') AS "database-is-auto-increment",
  is_generated = 'ALWAYS' AS "database-is-generated",
  is_nullable = 'YES' AS "database-is-nullable"
FROM information_schema.columns AS c
LEFT JOIN (SELECT tc.table_schema, tc.table_name, kc.column_name
           FROM information_schema.table_constraints AS tc
           INNER JOIN information_schema.key_column_usage AS kc
             ON (tc.constraint_name = kc.constraint_name) AND (tc.table_schema = kc.table_schema) AND (tc.table_name = kc.table_name)
           WHERE tc.constraint_type = 'PRIMARY KEY') AS pk
  ON (c.table_schema = pk.table_schema) AND (c.table_name = pk.table_name) AND (c.column_name = pk.column_name)
WHERE c.table_schema !~ '^information_schema|catalog_history|pg_' AND c.table_schema IN ('shop', 'billing')
UNION ALL
SELECT pa.attname AS name,
  CASE WHEN ptn.nspname IN ('public', 'pg_catalog') THEN format('%s', pt.typname)
       ELSE format('"%s"."%s"', ptn.nspname, pt.typname) END AS "database-type",
  pa.attnum - 1 AS "database-position", pn.nspname AS "table-schema", pc.relname AS "table-name",
  FALSE AS "pk?", col_description(pc.oid, pa.attnum) AS "field-comment", FALSE AS "database-required",
  NULL AS "database-default", FALSE AS "database-is-auto-increment", FALSE AS "database-is-generated",
  FALSE AS "database-is-nullable"
FROM pg_catalog.pg_class AS pc
INNER JOIN pg_catalog.pg_namespace AS pn ON pn.oid = pc.relnamespace
INNER JOIN pg_catalog.pg_attribute AS pa ON pa.attrelid = pc.oid
INNER JOIN pg_catalog.pg_type AS pt ON pt.oid = pa.atttypid
INNER JOIN pg_catalog.pg_namespace AS ptn ON ptn.oid = pt.typnamespace
WHERE (pc.relkind = 'm') AND (pa.attnum >= 1) AND pn.nspname IN ('shop', 'billing')
) x ORDER BY "table-schema" ASC, "table-name" ASC, "database-position" ASC
"""

METABASE_FKS = """
SELECT fk_ns.nspname AS "fk-table-schema", fk_table.relname AS "fk-table-name", fk_column.attname AS "fk-column-name",
       pk_ns.nspname AS "pk-table-schema", pk_table.relname AS "pk-table-name", pk_column.attname AS "pk-column-name"
FROM pg_constraint AS c
INNER JOIN pg_class AS fk_table ON c.conrelid = fk_table.oid
INNER JOIN pg_namespace AS fk_ns ON c.connamespace = fk_ns.oid
INNER JOIN pg_attribute AS fk_column ON c.conrelid = fk_column.attrelid
INNER JOIN pg_class AS pk_table ON c.confrelid = pk_table.oid
INNER JOIN pg_namespace AS pk_ns ON pk_table.relnamespace = pk_ns.oid
INNER JOIN pg_attribute AS pk_column ON c.confrelid = pk_column.attrelid
WHERE fk_ns.nspname !~ '^information_schema|catalog_history|pg_' AND (c.contype = 'f'::char)
  AND (fk_column.attnum = ANY(c.conkey)) AND (pk_column.attnum = ANY(c.confkey))
ORDER BY "fk-table-schema" ASC, "fk-table-name" ASC
"""

METABASE_INDEXES = """
SELECT tmp."table-schema", tmp."table-name",
       TRIM(BOTH '"' FROM pg_catalog.pg_get_indexdef(tmp.ci_oid, tmp.pos, false)) AS "field-name"
FROM (SELECT n.nspname AS "table-schema", ct.relname AS "table-name", ci.oid AS ci_oid,
             (information_schema._pg_expandarray(i.indkey)).n AS pos
      FROM pg_catalog.pg_class AS ct
      INNER JOIN pg_catalog.pg_namespace AS n ON ct.relnamespace = n.oid
      INNER JOIN pg_catalog.pg_index AS i ON ct.oid = i.indrelid
      INNER JOIN pg_catalog.pg_class AS ci ON ci.oid = i.indexrelid
      WHERE (pg_catalog.pg_get_expr(i.indpred, i.indrelid) IS NULL)
        AND n.nspname !~ '^information_schema|catalog_history|pg_' AND n.nspname IN ('shop')) AS tmp
WHERE tmp.pos = 1
"""

JDBC_TYPE_BY_OID = """
SELECT typinput='pg_catalog.array_in'::regproc as is_array, typtype, typname, pg_type.oid
  FROM pg_catalog.pg_type
  LEFT JOIN (select ns.oid as nspoid, ns.nspname, r.r
          from pg_namespace as ns
          join ( select s.r, (current_schemas(false))[s.r] as nspname
                   from generate_series(1, array_upper(current_schemas(false), 1)) as s(r) ) as r
         using ( nspname )
       ) as sp
    ON sp.nspoid = typnamespace
 WHERE pg_type.oid = %s
 ORDER BY sp.r, pg_type.oid DESC LIMIT 1
"""

JDBC_ALL_TYPES = """
SELECT t.typname, t.oid FROM pg_catalog.pg_type t
JOIN pg_catalog.pg_namespace n ON (t.typnamespace = n.oid)
WHERE n.nspname != 'pg_toast' AND (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
"""

JDBC_PRIMARY_KEYS = """
SELECT result.TABLE_CAT, result.TABLE_SCHEM, result.TABLE_NAME, result.COLUMN_NAME, result.KEY_SEQ, result.PK_NAME
FROM (SELECT NULL AS TABLE_CAT, n.nspname AS TABLE_SCHEM, ct.relname AS TABLE_NAME, a.attname AS COLUMN_NAME,
  (information_schema._pg_expandarray(i.indkey)).n AS KEY_SEQ, ci.relname AS PK_NAME,
  information_schema._pg_expandarray(i.indkey) AS KEYS, a.attnum AS A_ATTNUM
FROM pg_catalog.pg_class ct
JOIN pg_catalog.pg_attribute a ON (ct.oid = a.attrelid)
JOIN pg_catalog.pg_namespace n ON (ct.relnamespace = n.oid)
JOIN pg_catalog.pg_index i ON (a.attrelid = i.indrelid)
JOIN pg_catalog.pg_class ci ON (ci.oid = i.indexrelid)
WHERE true AND ct.relname = 'customers' AND i.indisprimary) result
WHERE result.A_ATTNUM = (result.KEYS).x
ORDER BY result.table_name, result.pk_name, result.key_seq
"""

METABASE_PRIVILEGES = """
with table_privileges as (
 select NULL as role, t.schemaname as schema, t.objectname as table,
   pg_catalog.has_any_column_privilege(current_user, '"' || replace(t.schemaname, '"', '""') || '"' || '.' || '"' || replace(t.objectname, '"', '""') || '"', 'update') as update,
   pg_catalog.has_any_column_privilege(current_user, '"' || replace(t.schemaname, '"', '""') || '"' || '.' || '"' || replace(t.objectname, '"', '""') || '"', 'select') as select,
   pg_catalog.has_any_column_privilege(current_user, '"' || replace(t.schemaname, '"', '""') || '"' || '.' || '"' || replace(t.objectname, '"', '""') || '"', 'insert') as insert,
   pg_catalog.has_table_privilege(     current_user, '"' || replace(t.schemaname, '"', '""') || '"' || '.' || '"' || replace(t.objectname, '"', '""') || '"', 'delete') as delete
 from (
   select schemaname, tablename as objectname from pg_catalog.pg_tables
   union
   select schemaname, viewname as objectname from pg_catalog.pg_views
   union
   select schemaname, matviewname as objectname from pg_catalog.pg_matviews
 ) t
 where t.schemaname !~ '^pg_'
   and t.schemaname <> 'information_schema'
   and pg_catalog.has_schema_privilege(current_user, t.schemaname, 'usage')
)
select t.*
from table_privileges t
"""

JDBC_TABLES = """
SELECT NULL AS TABLE_CAT, n.nspname AS TABLE_SCHEM, c.relname AS TABLE_NAME,
 CASE n.nspname ~ '^pg_' OR n.nspname = 'information_schema'
 WHEN true THEN CASE WHEN n.nspname = 'pg_catalog' OR n.nspname = 'information_schema' THEN
   CASE c.relkind WHEN 'r' THEN 'SYSTEM TABLE' WHEN 'v' THEN 'SYSTEM VIEW' WHEN 'i' THEN 'SYSTEM INDEX' ELSE NULL END
   ELSE NULL END
 WHEN false THEN CASE c.relkind WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'PARTITIONED TABLE' WHEN 'i' THEN 'INDEX'
   WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'c' THEN 'TYPE' WHEN 'f' THEN 'FOREIGN TABLE'
   WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE NULL END
 ELSE NULL END AS TABLE_TYPE, d.description AS REMARKS, '' AS TYPE_CAT, '' AS TYPE_SCHEM, '' AS TYPE_NAME,
 '' AS SELF_REFERENCING_COL_NAME, '' AS REF_GENERATION
FROM pg_catalog.pg_namespace n, pg_catalog.pg_class c
LEFT JOIN pg_catalog.pg_description d ON (c.oid = d.objoid AND d.objsubid = 0
  AND d.classoid = (SELECT oid FROM pg_catalog.pg_class WHERE relname = 'pg_class'
  AND relnamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = 'pg_catalog')))
WHERE c.relnamespace = n.oid AND n.nspname LIKE 'shop' AND c.relname LIKE '%%'
ORDER BY TABLE_TYPE, TABLE_SCHEM, TABLE_NAME
"""

GRAFANA_TABLES = """
SELECT quote_ident(table_name) AS "table" FROM information_schema.tables
WHERE quote_ident(table_schema) NOT IN ('information_schema', 'pg_catalog', '_timescaledb_cache',
  '_timescaledb_catalog', '_timescaledb_internal', '_timescaledb_config', 'timescaledb_information',
  'timescaledb_experimental') AND table_type = 'BASE TABLE'
  AND quote_ident(table_schema) IN (SELECT CASE WHEN TRIM(s[i]) = '"$user"' THEN user ELSE TRIM(s[i]) END
    FROM generate_series(array_lower(string_to_array(current_setting('search_path'), ','), 1),
         array_upper(string_to_array(current_setting('search_path'), ','), 1)) AS i,
    string_to_array(current_setting('search_path'), ',') s)
"""

GRAFANA_COLUMNS = """
SELECT quote_ident(column_name) AS "column", data_type AS "type"
FROM information_schema.columns WHERE quote_ident(table_name) = 'customers'
"""


def run(dsn, sql, params=None):
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, params).fetchall()


def test_metabase_lists_tables(dsn):
    rows = run(dsn, METABASE_TABLES)
    assert ("shop", "customers", "TABLE", None, None) in rows
    assert ("billing", "invoices", "TABLE", None, None) in rows
    assert not any(row[1] == "secrets" for row in rows)


def test_metabase_describes_fields(dsn):
    rows = run(dsn, METABASE_FIELDS)
    by_name = {(row[3], row[4], row[0]): row for row in rows}
    assert by_name[("shop", "customers", "id")][1] == "int8"
    assert by_name[("shop", "customers", "id")][5] is True  # pk?
    assert by_name[("shop", "customers", "name")][1] == "text"
    assert by_name[("billing", "invoices", "issued")][1] == "date"
    assert by_name[("billing", "invoices", "id")][5] is False


def test_metabase_sees_cross_source_foreign_key(dsn):
    assert ("billing", "invoices", "customer_id", "shop", "customers", "id") in [
        tuple(row[i] for i in range(6)) for row in _normalized_fks(dsn)
    ]


def _normalized_fks(dsn):
    with psycopg.connect(dsn) as conn:
        cursor = conn.execute(METABASE_FKS)
        names = [column.name for column in cursor.description]
        for row in cursor.fetchall():
            item = dict(zip(names, row))
            yield (item["fk-table-schema"], item["fk-table-name"], item["fk-column-name"],
                   item["pk-table-schema"], item["pk-table-name"], item["pk-column-name"])


def test_metabase_indexes(dsn):
    assert ("shop", "customers", "id") in run(dsn, METABASE_INDEXES)


def test_metabase_privilege_probe(dsn):
    assert run(dsn, 'SELECT TRUE FROM "shop"."customers" WHERE 1 <> 1 LIMIT 0') == []


def test_metabase_generated_query(dsn):
    rows = run(
        dsn,
        """SELECT CAST(DATE_TRUNC('month', "billing"."invoices"."issued") AS timestamp) AS "issued",
                  SUM("billing"."invoices"."amount") AS "sum"
           FROM "billing"."invoices"
           LEFT JOIN "shop"."customers" AS "Customers" ON "billing"."invoices"."customer_id" = "Customers"."id"
           WHERE "Customers"."country" = 'DE'
           GROUP BY CAST(DATE_TRUNC('month', "billing"."invoices"."issued") AS timestamp)
           ORDER BY CAST(DATE_TRUNC('month', "billing"."invoices"."issued") AS timestamp) ASC""",
    )
    assert [float(row[1]) for row in rows] == [10.5, 20.0]


@pytest.mark.parametrize("statement", [
    "SET extra_float_digits = 3",
    "SET application_name = 'PostgreSQL JDBC Driver'",
    "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY",
    "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ COMMITTED",
    "SET SESSION TIMEZONE TO 'UTC'",
    "SET ROLE analyst",
    "SHOW TRANSACTION ISOLATION LEVEL",
    "show timezone;",
    "select current_schema()",
    "SELECT nspname, typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.oid IN (SELECT DISTINCT enumtypid FROM pg_enum e)",
])
def test_session_statements(dsn, statement):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(statement)


def test_jdbc_type_lookup(dsn):
    assert run(dsn, JDBC_TYPE_BY_OID, (23,)) == [(False, "b", "int4", 23)]
    assert run(dsn, JDBC_TYPE_BY_OID, (1007,)) == [(True, "b", "_int4", 1007)]


def test_jdbc_all_types(dsn):
    assert ("int8", 20) in run(dsn, JDBC_ALL_TYPES)


def test_jdbc_primary_keys(dsn):
    assert run(dsn, JDBC_PRIMARY_KEYS) == [(None, "shop", "customers", "id", 1, "customers_pkey")]


def test_metabase_table_privileges_are_read_only(dsn):
    rows = {(row[1], row[2]): row[3:] for row in run(dsn, METABASE_PRIVILEGES)}
    # update, select, insert, delete
    assert rows[("shop", "customers")] == (False, True, False, False)


def test_jdbc_tables(dsn):
    rows = run(dsn, JDBC_TABLES)
    assert any(row[2] == "customers" and row[3] == "TABLE" for row in rows)


def test_jdbc_array_delimiter(dsn):
    assert run(dsn, "SELECT e.typdelim FROM pg_catalog.pg_type t, pg_catalog.pg_type e WHERE t.oid = 1007 and t.typelem = e.oid") == [(",",)]


def test_grafana_table_list(dsn):
    run(dsn, GRAFANA_TABLES)


def test_grafana_columns(dsn):
    assert ("id", "bigint") in run(dsn, GRAFANA_COLUMNS)
