"""An emulated pg_catalog and information_schema, kept in plain DuckDB tables.

BI tools discover tables with catalog queries written for PostgreSQL. DuckDB's
own pg_catalog views describe DuckDB, with every attached database and without
many columns clients read. These tables present the federated model instead:
one database, one schema per source, every object an ordinary table.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import types as t

PG_CATALOG_OID = 11
PUBLIC_OID = 2200
INFORMATION_SCHEMA_OID = 13000
OWNER_OID = 10
DATABASE_OID = 16384
HEAP_AM = 2
BTREE_AM = 403


@dataclass
class Column:
    name: str
    oid: int
    typmod: int = -1
    not_null: bool = False


@dataclass
class Table:
    schema: str
    name: str
    columns: list[Column]
    primary_key: list[str] = field(default_factory=list)


@dataclass
class ForeignKey:
    schema: str
    table: str
    column: str
    ref_schema: str
    ref_table: str
    ref_column: str


@dataclass
class Model:
    database: str
    tables: list[Table]
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    schemas: list[str] = field(default_factory=list)


TABLES: dict[str, str] = {
    "pg_namespace": "oid BIGINT, nspname VARCHAR, nspowner BIGINT, nspacl VARCHAR[]",
    "pg_class": """oid BIGINT, relname VARCHAR, relnamespace BIGINT, reltype BIGINT,
        reloftype BIGINT, relowner BIGINT, relam BIGINT, relfilenode BIGINT,
        reltablespace BIGINT, relpages INTEGER, reltuples FLOAT, relallvisible INTEGER,
        reltoastrelid BIGINT, relhasindex BOOLEAN, relisshared BOOLEAN,
        relpersistence VARCHAR, relkind VARCHAR, relnatts SMALLINT, relchecks SMALLINT,
        relhasrules BOOLEAN, relhastriggers BOOLEAN, relhassubclass BOOLEAN,
        relrowsecurity BOOLEAN, relforcerowsecurity BOOLEAN, relispopulated BOOLEAN,
        relreplident VARCHAR, relispartition BOOLEAN, relrewrite BIGINT,
        relfrozenxid BIGINT, relminmxid BIGINT, relacl VARCHAR[], reloptions VARCHAR[],
        relpartbound VARCHAR""",
    "pg_attribute": """attrelid BIGINT, attname VARCHAR, atttypid BIGINT,
        attstattarget INTEGER, attlen SMALLINT, attnum SMALLINT, attndims INTEGER,
        attcacheoff INTEGER, atttypmod INTEGER, attbyval BOOLEAN, attalign VARCHAR,
        attstorage VARCHAR, attcompression VARCHAR, attnotnull BOOLEAN, atthasdef BOOLEAN,
        atthasmissing BOOLEAN, attidentity VARCHAR, attgenerated VARCHAR,
        attisdropped BOOLEAN, attislocal BOOLEAN, attinhcount INTEGER, attcollation BIGINT,
        attacl VARCHAR[], attoptions VARCHAR[], attfdwoptions VARCHAR[], attmissingval VARCHAR""",
    "pg_type": """oid BIGINT, typname VARCHAR, typnamespace BIGINT, typowner BIGINT,
        typlen SMALLINT, typbyval BOOLEAN, typtype VARCHAR, typcategory VARCHAR,
        typispreferred BOOLEAN, typisdefined BOOLEAN, typdelim VARCHAR, typrelid BIGINT,
        typsubscript VARCHAR, typelem BIGINT, typarray BIGINT, typinput VARCHAR,
        typoutput VARCHAR, typreceive VARCHAR, typsend VARCHAR, typmodin VARCHAR,
        typmodout VARCHAR, typanalyze VARCHAR, typalign VARCHAR, typstorage VARCHAR,
        typnotnull BOOLEAN, typbasetype BIGINT, typtypmod INTEGER, typndims INTEGER,
        typcollation BIGINT, typdefaultbin VARCHAR, typdefault VARCHAR, typacl VARCHAR,
        pgf_sqlname VARCHAR""",
    "pg_index": """indexrelid BIGINT, indrelid BIGINT, indnatts SMALLINT, indnkeyatts SMALLINT,
        indisunique BOOLEAN, indnullsnotdistinct BOOLEAN, indisprimary BOOLEAN,
        indisexclusion BOOLEAN, indimmediate BOOLEAN, indisclustered BOOLEAN,
        indisvalid BOOLEAN, indcheckxmin BOOLEAN, indisready BOOLEAN, indislive BOOLEAN,
        indisreplident BOOLEAN, indkey SMALLINT[], indcollation BIGINT[], indclass BIGINT[],
        indoption SMALLINT[], indexprs VARCHAR, indpred VARCHAR""",
    "pg_constraint": """oid BIGINT, conname VARCHAR, connamespace BIGINT, contype VARCHAR,
        condeferrable BOOLEAN, condeferred BOOLEAN, convalidated BOOLEAN, conrelid BIGINT,
        contypid BIGINT, conindid BIGINT, conparentid BIGINT, confrelid BIGINT,
        confupdtype VARCHAR, confdeltype VARCHAR, confmatchtype VARCHAR, conislocal BOOLEAN,
        coninhcount INTEGER, connoinherit BOOLEAN, conkey SMALLINT[], confkey SMALLINT[],
        conpfeqop BIGINT[], conppeqop BIGINT[], conffeqop BIGINT[], conexclop BIGINT[],
        conbin VARCHAR, pgf_def VARCHAR""",
    "pg_description": "objoid BIGINT, classoid BIGINT, objsubid INTEGER, description VARCHAR",
    "pg_attrdef": "oid BIGINT, adrelid BIGINT, adnum SMALLINT, adbin VARCHAR",
    "pg_am": "oid BIGINT, amname VARCHAR, amhandler VARCHAR, amtype VARCHAR",
    "pg_database": """oid BIGINT, datname VARCHAR, datdba BIGINT, encoding INTEGER,
        datlocprovider VARCHAR, datistemplate BOOLEAN, datallowconn BOOLEAN,
        datconnlimit INTEGER, datfrozenxid BIGINT, datminmxid BIGINT, dattablespace BIGINT,
        datcollate VARCHAR, datctype VARCHAR, daticulocale VARCHAR, daticurules VARCHAR,
        datcollversion VARCHAR, datacl VARCHAR[]""",
    "pg_roles": """rolname VARCHAR, rolsuper BOOLEAN, rolinherit BOOLEAN, rolcreaterole BOOLEAN,
        rolcreatedb BOOLEAN, rolcanlogin BOOLEAN, rolreplication BOOLEAN,
        rolconnlimit INTEGER, rolpassword VARCHAR, rolvaliduntil TIMESTAMP,
        rolbypassrls BOOLEAN, rolconfig VARCHAR, oid BIGINT""",
    "pg_tablespace": "oid BIGINT, spcname VARCHAR, spcowner BIGINT, spcacl VARCHAR, spcoptions VARCHAR",
    "pg_settings": "name VARCHAR, setting VARCHAR, unit VARCHAR, category VARCHAR, short_desc VARCHAR",
    "pg_tables": """schemaname VARCHAR, tablename VARCHAR, tableowner VARCHAR, tablespace VARCHAR,
        hasindexes BOOLEAN, hasrules BOOLEAN, hastriggers BOOLEAN, rowsecurity BOOLEAN""",
    "pg_views": "schemaname VARCHAR, viewname VARCHAR, viewowner VARCHAR, definition VARCHAR",
    "pg_matviews": """schemaname VARCHAR, matviewname VARCHAR, matviewowner VARCHAR,
        tablespace VARCHAR, hasindexes BOOLEAN, ispopulated BOOLEAN, definition VARCHAR""",
    "pg_proc": """oid BIGINT, proname VARCHAR, pronamespace BIGINT, proowner BIGINT,
        prokind VARCHAR, prorettype BIGINT, proargtypes VARCHAR, prosrc VARCHAR""",
    "pg_collation": "oid BIGINT, collname VARCHAR, collnamespace BIGINT, collprovider VARCHAR",
    "pg_inherits": "inhrelid BIGINT, inhparent BIGINT, inhseqno INTEGER, inhdetachpending BOOLEAN",
    "pg_partitioned_table": "partrelid BIGINT, partstrat VARCHAR, partnatts SMALLINT",
    "pg_rewrite": "oid BIGINT, rulename VARCHAR, ev_class BIGINT, ev_type VARCHAR, ev_enabled VARCHAR",
    "pg_trigger": "oid BIGINT, tgrelid BIGINT, tgname VARCHAR, tgisinternal BOOLEAN, tgenabled VARCHAR",
    "pg_policy": """oid BIGINT, polname VARCHAR, polrelid BIGINT, polcmd VARCHAR, polpermissive BOOLEAN,
        polroles BIGINT[], polqual VARCHAR, polwithcheck VARCHAR""",
    "pg_extension": "oid BIGINT, extname VARCHAR, extowner BIGINT, extnamespace BIGINT, extversion VARCHAR",
    "pg_foreign_table": "ftrelid BIGINT, ftserver BIGINT, ftoptions VARCHAR",
    "pg_foreign_server": "oid BIGINT, srvname VARCHAR",
    "pg_publication": "oid BIGINT, pubname VARCHAR, puballtables BOOLEAN",
    "pg_publication_rel": "oid BIGINT, prpubid BIGINT, prrelid BIGINT, prqual VARCHAR, prattrs SMALLINT[]",
    "pg_publication_namespace": "oid BIGINT, pnpubid BIGINT, pnnspid BIGINT",
    "pg_publication_tables": "pubname VARCHAR, schemaname VARCHAR, tablename VARCHAR",
    "pg_statistic_ext": """oid BIGINT, stxrelid BIGINT, stxname VARCHAR, stxnamespace BIGINT, stxowner BIGINT,
        stxstattarget INTEGER, stxkeys SMALLINT[], stxkind VARCHAR[], stxexprs VARCHAR""",
    "pg_sequence": "seqrelid BIGINT, seqtypid BIGINT, seqstart BIGINT",
    "pg_depend": """classid BIGINT, objid BIGINT, objsubid INTEGER, refclassid BIGINT,
        refobjid BIGINT, refobjsubid INTEGER, deptype VARCHAR""",
    "pg_enum": "oid BIGINT, enumtypid BIGINT, enumsortorder FLOAT, enumlabel VARCHAR",
    "pg_range": "rngtypid BIGINT, rngsubtype BIGINT",
    "pg_stat_user_tables": "relid BIGINT, schemaname VARCHAR, relname VARCHAR, n_live_tup BIGINT",
    "pg_stat_activity": "datid BIGINT, datname VARCHAR, pid INTEGER, usename VARCHAR, state VARCHAR, query VARCHAR",
    "is_schemata": """catalog_name VARCHAR, schema_name VARCHAR, schema_owner VARCHAR,
        default_character_set_catalog VARCHAR, default_character_set_schema VARCHAR,
        default_character_set_name VARCHAR, sql_path VARCHAR""",
    "is_tables": """table_catalog VARCHAR, table_schema VARCHAR, table_name VARCHAR,
        table_type VARCHAR, self_referencing_column_name VARCHAR, reference_generation VARCHAR,
        user_defined_type_catalog VARCHAR, user_defined_type_schema VARCHAR,
        user_defined_type_name VARCHAR, is_insertable_into VARCHAR, is_typed VARCHAR,
        commit_action VARCHAR""",
    "is_views": "table_catalog VARCHAR, table_schema VARCHAR, table_name VARCHAR, view_definition VARCHAR",
    "is_columns": """table_catalog VARCHAR, table_schema VARCHAR, table_name VARCHAR,
        column_name VARCHAR, ordinal_position INTEGER, column_default VARCHAR,
        is_nullable VARCHAR, data_type VARCHAR, character_maximum_length INTEGER,
        character_octet_length INTEGER, numeric_precision INTEGER, numeric_precision_radix INTEGER,
        numeric_scale INTEGER, datetime_precision INTEGER, interval_type VARCHAR,
        udt_catalog VARCHAR, udt_schema VARCHAR, udt_name VARCHAR, is_identity VARCHAR,
        is_generated VARCHAR, is_updatable VARCHAR""",
    "is_table_constraints": """constraint_catalog VARCHAR, constraint_schema VARCHAR,
        constraint_name VARCHAR, table_catalog VARCHAR, table_schema VARCHAR, table_name VARCHAR,
        constraint_type VARCHAR, is_deferrable VARCHAR, initially_deferred VARCHAR,
        enforced VARCHAR""",
    "is_key_column_usage": """constraint_catalog VARCHAR, constraint_schema VARCHAR,
        constraint_name VARCHAR, table_catalog VARCHAR, table_schema VARCHAR, table_name VARCHAR,
        column_name VARCHAR, ordinal_position INTEGER, position_in_unique_constraint INTEGER""",
    "is_constraint_column_usage": """table_catalog VARCHAR, table_schema VARCHAR, table_name VARCHAR,
        column_name VARCHAR, constraint_catalog VARCHAR, constraint_schema VARCHAR,
        constraint_name VARCHAR""",
    "is_referential_constraints": """constraint_catalog VARCHAR, constraint_schema VARCHAR,
        constraint_name VARCHAR, unique_constraint_catalog VARCHAR,
        unique_constraint_schema VARCHAR, unique_constraint_name VARCHAR, match_option VARCHAR,
        update_rule VARCHAR, delete_rule VARCHAR""",
    "is_routines": "routine_catalog VARCHAR, routine_schema VARCHAR, routine_name VARCHAR, routine_type VARCHAR",
    "is_table_privileges": """grantor VARCHAR, grantee VARCHAR, table_catalog VARCHAR,
        table_schema VARCHAR, table_name VARCHAR, privilege_type VARCHAR, is_grantable VARCHAR""",
    "is_column_privileges": """grantor VARCHAR, grantee VARCHAR, table_catalog VARCHAR,
        table_schema VARCHAR, table_name VARCHAR, column_name VARCHAR, privilege_type VARCHAR,
        is_grantable VARCHAR""",
}

PG_NAMES = {name for name in TABLES if name.startswith("pg_")}
IS_NAMES = {name[3:] for name in TABLES if name.startswith("is_")}


def _columns(spec: str) -> list[tuple[str, str]]:
    columns = []
    for part in spec.replace("\n", " ").split(","):
        name, kind = part.split(None, 1)
        columns.append((name, kind.strip()))
    return columns


def _default(kind: str) -> Any:
    if kind == "BOOLEAN":
        return False
    if kind in {"BIGINT", "INTEGER", "SMALLINT", "FLOAT"}:
        return 0
    return None


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    return "'" + str(value).replace("'", "''") + "'"


class _Oids:
    """Stable object identifiers derived from names."""

    def __init__(self) -> None:
        self.used: dict[int, str] = {}
        self.names: dict[str, int] = {}

    def get(self, name: str) -> int:
        if name in self.names:
            return self.names[name]
        value = 100_000 + zlib.crc32(name.encode()) % 2_000_000_000
        while value in self.used:
            value += 1
        self.used[value] = name
        self.names[name] = value
        return value


def _rows(model: Model) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLES}
    oids = _Oids()
    db = model.database
    schemas = model.schemas or sorted({table.schema for table in model.tables})

    namespaces = {"pg_catalog": PG_CATALOG_OID, "public": PUBLIC_OID, "information_schema": INFORMATION_SCHEMA_OID}
    for schema in schemas:
        namespaces[schema] = oids.get(f"schema:{schema}")
    for schema, oid in namespaces.items():
        rows["pg_namespace"].append({"oid": oid, "nspname": schema, "nspowner": OWNER_OID})
        rows["is_schemata"].append({"catalog_name": db, "schema_name": schema, "schema_owner": "pgfederate"})

    for name, oid, array, length, byval, category, stem in t.PG_TYPES:
        base = dict(
            typnamespace=PG_CATALOG_OID,
            typowner=OWNER_OID,
            typlen=length,
            typbyval=byval,
            typcategory=category,
            typisdefined=True,
            typdelim=",",
            typalign="i",
            typstorage="p",
            typinput=f"{stem}in",
            typoutput=f"{stem}out",
            typreceive=f"{stem}recv",
            typsend=f"{stem}send",
            typmodin="-",
            typmodout="-",
            typanalyze="-",
            typsubscript="-",
        )
        rows["pg_type"].append(
            {**base, "oid": oid, "typname": name, "typtype": "b", "typarray": array,
             "pgf_sqlname": t.SQL_NAMES.get(oid, name)}
        )
        if array:
            rows["pg_type"].append(
                {**base, "oid": array, "typname": f"_{name}", "typtype": "b", "typcategory": "A",
                 "typlen": -1, "typbyval": False, "typelem": oid, "typinput": "array_in",
                 "typoutput": "array_out", "typreceive": "array_recv", "typsend": "array_send",
                 "typsubscript": "array_subscript_handler",
                 "pgf_sqlname": f"{t.SQL_NAMES.get(oid, name)}[]"}
            )

    rows["pg_am"] = [
        {"oid": HEAP_AM, "amname": "heap", "amtype": "t"},
        {"oid": BTREE_AM, "amname": "btree", "amtype": "i"},
    ]
    rows["pg_database"].append(
        {"oid": DATABASE_OID, "datname": db, "datdba": OWNER_OID, "encoding": 6, "datlocprovider": "c",
         "datallowconn": True, "datconnlimit": -1, "dattablespace": 1663, "datcollate": "C", "datctype": "C"}
    )
    rows["pg_roles"].append(
        {"rolname": "pgfederate", "rolinherit": True, "rolcanlogin": True, "rolconnlimit": -1, "oid": OWNER_OID}
    )
    rows["pg_tablespace"].append({"oid": 1663, "spcname": "pg_default", "spcowner": OWNER_OID})
    rows["pg_collation"].append({"oid": 100, "collname": "default", "collnamespace": PG_CATALOG_OID, "collprovider": "d"})

    positions: dict[tuple[str, str], dict[str, int]] = {}
    class_oids: dict[tuple[str, str], int] = {}

    for table in model.tables:
        oid = oids.get(f"table:{table.schema}.{table.name}")
        namespace = namespaces[table.schema]
        class_oids[(table.schema, table.name)] = oid
        positions[(table.schema, table.name)] = {c.name: i + 1 for i, c in enumerate(table.columns)}
        rows["pg_class"].append(
            {"oid": oid, "relname": table.name, "relnamespace": namespace, "relowner": OWNER_OID,
             "relam": HEAP_AM, "relpersistence": "p", "relkind": "r", "relnatts": len(table.columns),
             "relhasindex": bool(table.primary_key), "relispopulated": True, "relreplident": "d",
             "reltuples": -1}
        )
        rows["pg_tables"].append(
            {"schemaname": table.schema, "tablename": table.name, "tableowner": "pgfederate",
             "hasindexes": bool(table.primary_key)}
        )
        rows["is_tables"].append(
            {"table_catalog": db, "table_schema": table.schema, "table_name": table.name,
             "table_type": "BASE TABLE", "is_insertable_into": "NO", "is_typed": "NO"}
        )
        rows["is_table_privileges"].append(
            {"grantor": "pgfederate", "grantee": "PUBLIC", "table_catalog": db,
             "table_schema": table.schema, "table_name": table.name, "privilege_type": "SELECT",
             "is_grantable": "NO"}
        )
        for number, column in enumerate(table.columns, 1):
            type_name = t.TYPE_NAMES.get(column.oid, "text")
            rows["pg_attribute"].append(
                {"attrelid": oid, "attname": column.name, "atttypid": column.oid, "attstattarget": -1,
                 "attlen": t.typlen(column.oid), "attnum": number, "attcacheoff": -1,
                 "atttypmod": column.typmod, "attnotnull": column.not_null, "attislocal": True,
                 "attalign": "i", "attstorage": "p", "attcompression": "", "attidentity": "",
                 "attgenerated": ""}
            )
            rows["is_columns"].append(
                {"table_catalog": db, "table_schema": table.schema, "table_name": table.name,
                 "column_name": column.name, "ordinal_position": number,
                 "is_nullable": "NO" if column.not_null else "YES",
                 "data_type": t.SQL_NAMES.get(column.oid, type_name), "udt_catalog": db,
                 "udt_schema": "pg_catalog", "udt_name": type_name, "is_identity": "NO",
                 "is_generated": "NEVER", "is_updatable": "NO",
                 "numeric_precision": None, "numeric_scale": None}
            )
        if table.primary_key:
            _primary_key(rows, oids, table, oid, namespace, db, positions)

    for number, key in enumerate(model.foreign_keys):
        source = class_oids.get((key.schema, key.table))
        target = class_oids.get((key.ref_schema, key.ref_table))
        left = positions.get((key.schema, key.table), {}).get(key.column)
        right = positions.get((key.ref_schema, key.ref_table), {}).get(key.ref_column)
        if not (source and target and left and right):
            continue  # a relation can point at a table that is not loaded yet
        name = f"{key.table}_{key.column}_fkey{number}"
        rows["pg_constraint"].append(
            {"oid": oids.get(f"fk:{key.schema}.{name}"), "conname": name,
             "connamespace": namespaces[key.schema], "contype": "f", "convalidated": True,
             "conrelid": source, "confrelid": target, "confupdtype": "a", "confdeltype": "a",
             "confmatchtype": "s", "conislocal": True, "conkey": [left], "confkey": [right],
             "pgf_def": f'FOREIGN KEY ({key.column}) REFERENCES {key.ref_schema}.{key.ref_table}({key.ref_column})'}
        )
        rows["is_table_constraints"].append(
            {"constraint_catalog": db, "constraint_schema": key.schema, "constraint_name": name,
             "table_catalog": db, "table_schema": key.schema, "table_name": key.table,
             "constraint_type": "FOREIGN KEY", "is_deferrable": "NO", "initially_deferred": "NO",
             "enforced": "YES"}
        )
        rows["is_key_column_usage"].append(
            {"constraint_catalog": db, "constraint_schema": key.schema, "constraint_name": name,
             "table_catalog": db, "table_schema": key.schema, "table_name": key.table,
             "column_name": key.column, "ordinal_position": 1, "position_in_unique_constraint": 1}
        )
        rows["is_constraint_column_usage"].append(
            {"table_catalog": db, "table_schema": key.ref_schema, "table_name": key.ref_table,
             "column_name": key.ref_column, "constraint_catalog": db, "constraint_schema": key.schema,
             "constraint_name": name}
        )
        rows["is_referential_constraints"].append(
            {"constraint_catalog": db, "constraint_schema": key.schema, "constraint_name": name,
             "unique_constraint_catalog": db, "unique_constraint_schema": key.ref_schema,
             "unique_constraint_name": f"{key.ref_table}_pkey", "match_option": "NONE",
             "update_rule": "NO ACTION", "delete_rule": "NO ACTION"}
        )
    return rows


def _primary_key(rows, oids, table, oid, namespace, db, positions) -> None:
    index_name = f"{table.name}_pkey"
    index_oid = oids.get(f"index:{table.schema}.{index_name}")
    keys = [positions[(table.schema, table.name)][c] for c in table.primary_key]
    rows["pg_class"].append(
        {"oid": index_oid, "relname": index_name, "relnamespace": namespace, "relowner": OWNER_OID,
         "relam": BTREE_AM, "relpersistence": "p", "relkind": "i", "relnatts": len(keys),
         "relispopulated": True, "relreplident": "n", "reltuples": -1}
    )
    rows["pg_index"].append(
        {"indexrelid": index_oid, "indrelid": oid, "indnatts": len(keys), "indnkeyatts": len(keys),
         "indisunique": True, "indisprimary": True, "indimmediate": True, "indisvalid": True,
         "indisready": True, "indislive": True, "indkey": keys, "indcollation": [0] * len(keys),
         "indclass": [0] * len(keys), "indoption": [0] * len(keys)}
    )
    rows["pg_constraint"].append(
        {"oid": oids.get(f"pk:{table.schema}.{index_name}"), "conname": index_name,
         "connamespace": namespace, "contype": "p", "convalidated": True, "conrelid": oid,
         "conindid": index_oid, "conislocal": True, "conkey": keys,
         "pgf_def": f"PRIMARY KEY ({', '.join(table.primary_key)})"}
    )
    rows["is_table_constraints"].append(
        {"constraint_catalog": db, "constraint_schema": table.schema, "constraint_name": index_name,
         "table_catalog": db, "table_schema": table.schema, "table_name": table.name,
         "constraint_type": "PRIMARY KEY", "is_deferrable": "NO", "initially_deferred": "NO",
         "enforced": "YES"}
    )
    for position, column in enumerate(table.primary_key, 1):
        rows["is_key_column_usage"].append(
            {"constraint_catalog": db, "constraint_schema": table.schema, "constraint_name": index_name,
             "table_catalog": db, "table_schema": table.schema, "table_name": table.name,
             "column_name": column, "ordinal_position": position}
        )
        rows["is_constraint_column_usage"].append(
            {"table_catalog": db, "table_schema": table.schema, "table_name": table.name,
             "column_name": column, "constraint_catalog": db, "constraint_schema": table.schema,
             "constraint_name": index_name}
        )


def _chunks(values: list[str], size: int = 500) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def create_tables(cursor: Any, schema: str) -> None:
    cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    for name, spec in TABLES.items():
        cursor.execute(f'CREATE TABLE IF NOT EXISTS "{schema}"."{name}" ({spec})')


def publish(cursor: Any, schema: str, model: Model) -> None:
    """Replace the content of one catalog schema in a single transaction."""
    rows = _rows(model)
    cursor.execute("BEGIN")
    try:
        for name, spec in TABLES.items():
            cursor.execute(f'DELETE FROM "{schema}"."{name}"')
            columns = _columns(spec)
            values = []
            for row in rows[name]:
                cells = [_literal(row.get(column, _default(kind))) for column, kind in columns]
                values.append("(" + ", ".join(cells) + ")")
            for chunk in _chunks(values):
                cursor.execute(f'INSERT INTO "{schema}"."{name}" VALUES ' + ", ".join(chunk))
        cursor.execute("COMMIT")
    except Exception:
        cursor.execute("ROLLBACK")
        raise


MACROS = [
    # regclass input and output
    """CREATE OR REPLACE MACRO __pgf_regclass(x) AS (
        SELECT c.oid FROM __pg.pg_class c JOIN __pg.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = regexp_replace(replace(CAST(x AS VARCHAR), '"', ''), '^.*\\.', '')
          AND (NOT contains(CAST(x AS VARCHAR), '.')
               OR n.nspname = split_part(replace(CAST(x AS VARCHAR), '"', ''), '.', 1))
        LIMIT 1)""",
    "CREATE OR REPLACE MACRO __pgf_regclass_name(x) AS (SELECT relname FROM __pg.pg_class WHERE oid = x LIMIT 1)",
    "CREATE OR REPLACE MACRO __pgf_regtype(x) AS (SELECT oid FROM __pg.pg_type WHERE typname = CAST(x AS VARCHAR) OR pgf_sqlname = CAST(x AS VARCHAR) LIMIT 1)",
    "CREATE OR REPLACE MACRO __pgf_format_type(x, m) AS (SELECT pgf_sqlname FROM __pg.pg_type WHERE oid = x LIMIT 1)",
    """CREATE OR REPLACE MACRO __pgf_indexdef(x, n) AS (
        SELECT a.attname FROM __pg.pg_index i JOIN __pg.pg_attribute a
          ON a.attrelid = i.indrelid AND a.attnum = i.indkey[n]
        WHERE i.indexrelid = x LIMIT 1)""",
    "CREATE OR REPLACE MACRO __pgf_positions(a) AS range(1, len(a) + 1)",
    "CREATE OR REPLACE MACRO __pgf_expand(a) AS list_transform(range(1, len(a) + 1), lambda i: {'x': a[i], 'n': i})",
    """CREATE OR REPLACE MACRO quote_ident(x) AS CASE WHEN regexp_full_match(x, '[a-z_][a-z0-9_]*') THEN x
        ELSE '"' || replace(x, '"', '""') || '"' END""",
    "CREATE OR REPLACE MACRO array_upper(a, d) AS len(a)",
    "CREATE OR REPLACE MACRO array_lower(a, d) AS CASE WHEN len(a) > 0 THEN 1 END",
    "CREATE OR REPLACE MACRO generate_subscripts(a, d) AS TABLE SELECT unnest(range(1, len(a) + 1)) AS generate_subscripts",
    "CREATE OR REPLACE MACRO __pgf_constraintdef(x) AS (SELECT pgf_def FROM __pg.pg_constraint WHERE oid = x LIMIT 1)",
]
