"""Mapping between DuckDB types and PostgreSQL type OIDs."""

from __future__ import annotations

BOOL, BYTEA, NAME, INT8, INT2, INT4, TEXT, OID = 16, 17, 19, 20, 21, 23, 25, 26
JSON, FLOAT4, FLOAT8, VARCHAR, DATE, TIME = 114, 700, 701, 1043, 1082, 1083
TIMESTAMP, TIMESTAMPTZ, INTERVAL, NUMERIC, UUID, CHAR = 1114, 1184, 1186, 1700, 2950, 18
UNKNOWN, REGCLASS, REGTYPE, REGPROC = 705, 2205, 2206, 24

# name, oid, array oid, typlen, byval, category, input function stem
PG_TYPES = [
    ("bool", BOOL, 1000, 1, True, "B", "bool"),
    ("bytea", BYTEA, 1001, -1, False, "U", "bytea"),
    ("char", CHAR, 1002, 1, True, "Z", "char"),
    ("name", NAME, 1003, 64, False, "S", "name"),
    ("int8", INT8, 1016, 8, True, "N", "int8"),
    ("int2", INT2, 1005, 2, True, "N", "int2"),
    ("int4", INT4, 1007, 4, True, "N", "int4"),
    ("regproc", REGPROC, 1008, 4, True, "N", "regproc"),
    ("text", TEXT, 1009, -1, False, "S", "text"),
    ("oid", OID, 1028, 4, True, "N", "oid"),
    ("json", JSON, 199, -1, False, "U", "json"),
    ("float4", FLOAT4, 1021, 4, True, "N", "float4"),
    ("float8", FLOAT8, 1022, 8, True, "N", "float8"),
    ("unknown", UNKNOWN, 0, -2, False, "X", "unknown"),
    ("varchar", VARCHAR, 1015, -1, False, "S", "varchar"),
    ("date", DATE, 1182, 4, True, "D", "date"),
    ("time", TIME, 1183, 8, True, "D", "time"),
    ("timestamp", TIMESTAMP, 1115, 8, True, "D", "timestamp"),
    ("timestamptz", TIMESTAMPTZ, 1185, 8, True, "D", "timestamptz"),
    ("interval", INTERVAL, 1187, 16, False, "T", "interval"),
    ("numeric", NUMERIC, 1231, -1, False, "N", "numeric"),
    ("uuid", UUID, 2951, 16, False, "U", "uuid"),
    ("regclass", REGCLASS, 2210, 4, True, "N", "regclass"),
    ("regtype", REGTYPE, 2211, 4, True, "N", "regtype"),
]

TYPE_NAMES = {oid: name for name, oid, *_ in PG_TYPES}
ARRAY_TYPES = {name: array for name, _, array, *_ in PG_TYPES if array}
SQL_NAMES = {
    BOOL: "boolean",
    INT2: "smallint",
    INT4: "integer",
    INT8: "bigint",
    FLOAT4: "real",
    FLOAT8: "double precision",
    VARCHAR: "character varying",
    TIMESTAMP: "timestamp without time zone",
    TIMESTAMPTZ: "timestamp with time zone",
    TIME: "time without time zone",
}

_SCALARS = {
    "BOOLEAN": BOOL,
    "TINYINT": INT2,
    "SMALLINT": INT2,
    "UTINYINT": INT2,
    "INTEGER": INT4,
    "USMALLINT": INT4,
    "BIGINT": INT8,
    "UINTEGER": INT8,
    "UBIGINT": NUMERIC,
    "HUGEINT": NUMERIC,
    "UHUGEINT": NUMERIC,
    "FLOAT": FLOAT4,
    "DOUBLE": FLOAT8,
    "VARCHAR": TEXT,
    "BLOB": BYTEA,
    "BIT": TEXT,
    "DATE": DATE,
    "TIME": TIME,
    "TIME WITH TIME ZONE": TIME,
    "TIMESTAMP": TIMESTAMP,
    "TIMESTAMP_S": TIMESTAMP,
    "TIMESTAMP_MS": TIMESTAMP,
    "TIMESTAMP_NS": TIMESTAMP,
    "TIMESTAMP WITH TIME ZONE": TIMESTAMPTZ,
    "INTERVAL": INTERVAL,
    "UUID": UUID,
    "JSON": JSON,
}


def oid_for(duck_type: object) -> int:
    """Return the PostgreSQL OID a DuckDB type is shown as."""
    name = str(duck_type).upper()
    if name.startswith("DECIMAL"):
        return NUMERIC
    if name in _SCALARS:
        return _SCALARS[name]
    if name.endswith("]") or name.startswith(("STRUCT", "MAP", "UNION")):
        return JSON
    return TEXT  # ENUM and anything else is shown as text


def typlen(oid: int) -> int:
    for _, type_oid, _, length, *_ in PG_TYPES:
        if type_oid == oid:
            return length
    return -1
