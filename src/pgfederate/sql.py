"""Translate PostgreSQL statements into safe DuckDB statements.

Every statement passes through here. Anything that is not a plain read of the
exposed tables, or that cannot be parsed, is refused: the allow-list works on the
parsed tree, so a client cannot reach attached databases, DuckDB's system
functions or the file system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from . import catalog
from . import types as t


class SqlError(Exception):
    def __init__(self, message: str, code: str = "42601") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Context:
    """What the rewrite needs to know about the current session."""

    database: str
    user: str
    allowed: set[str]
    tables: dict[str, list[str]]  # table name -> schemas that have it
    catalog_schema: str = "__pg"
    pid: int = 1
    settings: dict[str, str] = field(default_factory=dict)
    version: str = "PostgreSQL 16.0 (pgfederate)"


@dataclass
class Statement:
    kind: str  # query, set, show, begin, commit, rollback, empty, noop
    tree: Any = None
    tag: str = ""
    name: str = ""
    value: str = ""
    param_count: int = 0


TABLE_FUNCTIONS = {"unnest", "explode", "generate_series", "range", "generate_subscripts"}
SCALAR_TABLE_FUNCTIONS = {"string_to_array", "split", "str_split", "regexp_split_to_array"}
# Functions a client may call only in their emulated form. Everything with these
# prefixes is DuckDB machinery or reaches outside the exposed tables.
DENIED_FUNCTION = re.compile(
    r"^(read_|scan_|duckdb_|pragma_|parquet_|sniff_|glob$|getenv$|query$|query_table$|which_secret$"
    r"|sql_auto_complete$|load_|install_|postgres_|mysql_|sqlite_|pg_clear|mysql_clear|checkpoint$"
    r"|force_checkpoint$|enable_|disable_|set_|shell$|sqlite$|icu_|test_|summary$|seq_scan$|repeat_row$)"
)
CONSTANT_FUNCTIONS: dict[str, Any] = {
    "pg_table_is_visible": True,
    "pg_type_is_visible": True,
    "pg_function_is_visible": True,
    "pg_collation_is_visible": True,
    "pg_is_in_recovery": False,
    "pg_encoding_to_char": "UTF8",
    "getdatabaseencoding": "UTF8",
    "pg_get_expr": None,
    "pg_get_viewdef": None,
    "pg_get_serial_sequence": None,
    "pg_get_partkeydef": None,
    "pg_get_statisticsobjdef_columns": None,
    "obj_description": None,
    "col_description": None,
    "shobj_description": None,
    "pg_relation_size": 0,
    "pg_total_relation_size": 0,
    "pg_table_size": 0,
    "pg_indexes_size": 0,
    "pg_database_size": 0,
    "txid_current": 1,
    "pg_tablespace_location": "",
    "pg_get_function_result": None,
    "pg_get_function_arguments": None,
    "pg_get_function_identity_arguments": None,
    "pg_get_triggerdef": None,
    "pg_get_ruledef": None,
    "pg_relation_is_publishable": False,
}
PRIVILEGE_FUNCTIONS = {
    "has_table_privilege",
    "has_schema_privilege",
    "has_column_privilege",
    "has_any_column_privilege",
    "has_database_privilege",
    "has_sequence_privilege",
    "has_function_privilege",
}
READ_PRIVILEGES = {"select", "usage", "connect", "execute"}
MACRO_FUNCTIONS = {
    "format_type": ("__pgf_format_type", 2),
    "pg_get_constraintdef": ("__pgf_constraintdef", 1),
    "pg_get_indexdef": ("__pgf_indexdef", 2),
}
OID_TYPES = {"regclass", "regtype", "regproc", "regprocedure", "regnamespace", "regrole", "regoper", "oid", "xid", "cid"}
TEXT_TYPES = {"name", "char", '"char"', "bpchar", "cstring"}
READ_TYPES = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery, exp.Values)
CONTROL = re.compile(
    r"^\s*(?P<word>begin|start\s+transaction|commit|end|rollback|abort|savepoint|release|set|reset|show"
    r"|discard|deallocate|close|unlisten|listen|declare|fetch|prepare|execute)\b(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)


KEYWORD_ALIAS = re.compile(
    r"\b(as)\s+(select|insert|update|delete|table|schema|role|user|from|where|order|group)(?=\s*(?:,|\n|$))",
    re.IGNORECASE,
)


def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons, respecting strings and comments."""
    parts: list[str] = []
    start, i, n = 0, 0, len(sql)
    while i < n:
        char = sql[i]
        if char in "'\"":
            i += 1
            while i < n:
                if sql[i] == char:
                    if i + 1 < n and sql[i + 1] == char:
                        i += 1
                    else:
                        break
                i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 1
        elif char == "$":
            match = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if match:
                end = sql.find(match.group(0), i + len(match.group(0)))
                i = n if end < 0 else end + len(match.group(0)) - 1
        elif char == ";":
            parts.append(sql[start:i])
            start = i + 1
        i += 1
    parts.append(sql[start:])
    return [part for part in parts if _strip_comments(part).strip()]


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def _control(sql: str) -> Statement | None:
    match = CONTROL.match(_strip_comments(sql))
    if not match:
        return None
    word = " ".join(match.group("word").lower().split())
    rest = match.group("rest").strip()
    if word in {"begin", "start transaction"}:
        return Statement("begin", tag="BEGIN")
    if word in {"commit", "end"}:
        return Statement("commit", tag="COMMIT")
    if word in {"rollback", "abort"}:
        if rest.lower().startswith("to"):
            return Statement("noop", tag="ROLLBACK")
        return Statement("rollback", tag="ROLLBACK")
    if word == "savepoint":
        return Statement("noop", tag="SAVEPOINT")
    if word == "release":
        return Statement("noop", tag="RELEASE")
    if word == "show":
        return Statement("show", name=rest.lower().strip('"'), tag="SHOW")
    if word == "set":
        return _set(rest)
    if word == "reset":
        return Statement("noop", tag="RESET")
    if word == "discard":
        return Statement("noop", tag="DISCARD ALL")
    if word == "deallocate":
        return Statement("noop", tag="DEALLOCATE")
    if word == "close":
        return Statement("noop", tag="CLOSE CURSOR")
    if word == "unlisten":
        return Statement("noop", tag="UNLISTEN")
    raise SqlError(f"{word.upper()} is not supported by pgfederate", "0A000")


def _set(rest: str) -> Statement:
    rest = re.sub(r"^(session|local)\s+", "", rest, flags=re.IGNORECASE)
    if re.match(r"(characteristics|transaction|session\s+characteristics)\b", rest, re.IGNORECASE):
        return Statement("noop", tag="SET")
    zone = re.match(r"time\s+zone\s+(.*)$", rest, re.IGNORECASE | re.DOTALL)
    if zone:
        return Statement("set", name="timezone", value=zone.group(1).strip().strip("'\""), tag="SET")
    assignment = re.match(r"([\w.\"]+)\s*(?:=|\s+to\s+)\s*(.*)$", rest, re.IGNORECASE | re.DOTALL)
    if not assignment:
        return Statement("noop", tag="SET")
    return Statement(
        "set",
        name=assignment.group(1).strip('"').lower(),
        value=assignment.group(2).strip().strip("'\""),
        tag="SET",
    )


def parse(sql: str, context: Context) -> list[Statement]:
    """Turn a query string into statements ready for execution."""
    pieces = split_statements(sql)
    if not pieces:
        return [Statement("empty")]
    return [parse_one(piece, context) for piece in pieces]


def parse_one(sql: str, context: Context) -> Statement:
    control = _control(sql)
    if control is not None:
        return control
    explain = re.match(r"^\s*explain\b(?:\s+analyze\b)?(.*)$", _strip_comments(sql), re.IGNORECASE | re.DOTALL)
    body = explain.group(1) if explain else sql
    try:
        tree = _parse_tree(body)
    except (ParseError, TokenError) as error:
        raise SqlError(f"cannot parse statement: {str(error).splitlines()[0]}") from error
    if not _is_read(tree):
        raise SqlError("pgfederate is read-only: only SELECT is allowed", "25006")
    tree = rewrite(tree, context)
    return Statement(
        "explain" if explain else "query",
        tree=tree,
        tag="EXPLAIN" if explain else "SELECT",
        param_count=_param_count(tree),
    )


def _parse_tree(body: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(body, read="postgres")
    except ParseError:
        # PostgreSQL allows keywords after AS without quotes, e.g. "... AS select,".
        quoted = KEYWORD_ALIAS.sub(lambda m: f'{m.group(1)} "{m.group(2)}"', body)
        if quoted == body:
            raise
        return sqlglot.parse_one(quoted, read="postgres")


def _is_read(tree: exp.Expression) -> bool:
    if not isinstance(tree, READ_TYPES):
        return False
    return tree.find(exp.Into, exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Lock) is None


def _param_count(tree: exp.Expression) -> int:
    numbers = [_param_number(node) for node in tree.find_all(exp.Parameter)]
    return max(numbers, default=0)


def _param_number(node: exp.Parameter) -> int:
    return int(node.this.name if isinstance(node.this, exp.Expression) else node.this)


def rewrite(tree: exp.Expression, context: Context) -> exp.Expression:
    ctes = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
    _name_function_columns(tree)
    for node in reversed(list(tree.walk())):  # children before their parents
        replacement = _normalize(node, context)
        if replacement is not node:
            if node is tree:
                tree = replacement
            else:
                node.replace(replacement)
    for table in list(tree.find_all(exp.Table)):
        _rewrite_table(table, context, ctes)
    return tree


def _name_function_columns(tree: exp.Expression) -> None:
    """Result columns are named after the function, as in PostgreSQL (count, not count_star())."""
    if not isinstance(tree, exp.Select):
        return
    for expression in list(tree.expressions):
        if not isinstance(expression, exp.Func):
            continue
        if isinstance(expression, (exp.Cast, exp.Binary, exp.Unary, exp.Case, exp.If)):
            continue
        name = _function_name(expression)
        if name:
            expression.replace(exp.alias_(expression.copy(), name, quoted=True))


def _text(value: Any) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(value)


def _function_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return None


def _normalize(node: exp.Expression, context: Context) -> exp.Expression:
    if isinstance(node, exp.Dot) and isinstance(node.this, (exp.Identifier, exp.Var, exp.Column)):
        if node.this.name.lower() in {"pg_catalog", "information_schema"}:
            return node.expression  # already rewritten, children go first
    if isinstance(node, exp.Dot) and isinstance(node.this, exp.Paren):
        expanded = _expand_array(node)
        if expanded is not None:
            return expanded
    if isinstance(node, exp.Collate):
        return node.this
    if isinstance(node, exp.Operator):
        return _operator(node)
    if isinstance(node, exp.Cast):
        return _cast(node)
    if isinstance(node, exp.CurrentUser):
        return _text(context.user)
    if isinstance(node, exp.Func):
        return _function(node, context)
    if isinstance(node, exp.Column) and not node.table and node.name.lower() in {
        "current_user", "session_user", "user", "current_catalog", "current_schema", "current_role",
    }:
        if isinstance(node.this, exp.Identifier) and node.this.quoted:
            return node
        return _constant_word(node.name.lower(), context)
    return node


def _expand_array(node: exp.Dot) -> exp.Expression | None:
    """(information_schema._pg_expandarray(a)).x and .n, as used for index keys."""
    inner = node.this.this
    if not (isinstance(inner, exp.Anonymous) and str(inner.this).lower() == "unnest"):
        return None
    expand = inner.expressions[0] if inner.expressions else None
    if not (isinstance(expand, exp.Anonymous) and str(expand.this) == "__pgf_expand"):
        return None
    array = expand.expressions[0]
    if node.expression.name == "x":
        return exp.Anonymous(this="unnest", expressions=[array])
    span = exp.Anonymous(this="__pgf_positions", expressions=[array.copy()])
    return exp.Anonymous(this="unnest", expressions=[span])


def _constant_word(word: str, context: Context) -> exp.Expression:
    if word == "current_catalog":
        return _text(context.database)
    if word == "current_schema":
        return _text("public")
    return _text(context.user)


def _operator(node: exp.Operator) -> exp.Expression:
    symbol = str(node.args["operator"]).rsplit(".", 1)[-1].lower() if node.args.get("operator") else ""
    left, right = node.this, node.expression
    if symbol == "~":
        return exp.RegexpLike(this=left, expression=right)
    if symbol == "!~":
        return exp.Not(this=exp.RegexpLike(this=left, expression=right))
    if symbol == "~*":
        return exp.RegexpILike(this=left, expression=right)
    if symbol == "=":
        return exp.EQ(this=left, expression=right)
    raise SqlError(f"operator {symbol} is not supported", "42883")


def _cast(node: exp.Cast) -> exp.Expression:
    target = node.to.sql("postgres").lower().replace("pg_catalog.", "").replace('"', "")
    value = node.this
    if target in {"regclass", "regtype"}:
        macro = "__pgf_regclass" if target == "regclass" else "__pgf_regtype"
        if isinstance(value, exp.Literal) and value.is_string or isinstance(value, exp.Parameter):
            return exp.Anonymous(this=macro, expressions=[value])
        if target == "regclass":
            return exp.Anonymous(this="__pgf_regclass_name", expressions=[value])
        return value
    if target == "regproc" or target == "regprocedure":
        if isinstance(value, exp.Literal):
            return exp.Literal.string(value.name.replace("pg_catalog.", ""))
        return value
    if target in OID_TYPES:
        return exp.Cast(this=value, to=exp.DataType.build("BIGINT"))
    if target in TEXT_TYPES:
        return exp.Cast(this=value, to=exp.DataType.build("VARCHAR"))
    if target in {"oid[]", "int2vector", "oidvector"}:
        return value
    if "pg_catalog." in node.to.sql("postgres").lower():
        return exp.Cast(this=value, to=exp.DataType.build(target, dialect="postgres"))
    return node


def _arguments(node: exp.Func) -> list[exp.Expression]:
    arguments: list[exp.Expression] = []
    for value in node.args.values():
        if isinstance(value, exp.Expression):
            arguments.append(value)
        elif isinstance(value, list):
            arguments.extend(item for item in value if isinstance(item, exp.Expression))
    return arguments


def _format(node: exp.Func) -> exp.Expression:
    """PostgreSQL format() is printf-like; DuckDB's format() is not."""
    arguments = _arguments(node)
    if arguments and isinstance(arguments[0], exp.Literal):
        text = re.sub(r"%[IL]", "%s", arguments[0].name)
        arguments[0] = exp.Literal.string(text)
    return exp.Anonymous(this="printf", expressions=arguments)


def _function(node: exp.Func, context: Context) -> exp.Expression:
    name = _function_name(node) or ""
    if name == "format":
        return _format(node)
    if name in PRIVILEGE_FUNCTIONS:
        # The gateway is read-only; tools such as Metabase ask before offering writes.
        arguments = _arguments(node)
        wanted = arguments[-1].name.lower() if arguments else ""
        return _text(any(word.strip() in READ_PRIVILEGES for word in wanted.split(",")))
    if name == "_pg_expandarray":
        expand = exp.Anonymous(this="__pgf_expand", expressions=_arguments(node))
        return exp.Anonymous(this="unnest", expressions=[expand])
    if name in MACRO_FUNCTIONS:
        macro, arity = MACRO_FUNCTIONS[name]
        return exp.Anonymous(this=macro, expressions=_arguments(node)[:arity])
    if name in CONSTANT_FUNCTIONS:
        return _text(CONSTANT_FUNCTIONS[name])
    if name in {"current_database", "current_catalog"}:
        return _text(context.database)
    if name in {"current_schema"}:
        return _text("public")
    if name == "version":
        return _text(context.version)
    if name == "pg_backend_pid":
        return _text(context.pid)
    if name == "pg_get_userbyid":
        return _text("pgfederate")
    if name == "current_schemas":
        return exp.Array(expressions=[_text("pg_catalog"), _text("public")])
    if name == "current_setting":
        arguments = _arguments(node)
        return _text(context.settings.get(arguments[0].name.lower() if arguments else "", ""))
    if name == "set_config":
        arguments = _arguments(node)
        return arguments[1] if len(arguments) > 1 else exp.Null()
    if name in {"pg_postmaster_start_time", "pg_conf_load_time"}:
        return exp.CurrentTimestamp()
    return node


def _rewrite_table(table: exp.Table, context: Context, ctes: set[str]) -> None:
    source = table.this
    if isinstance(source, exp.Func):
        name = _function_name(source) or ""
        if name in SCALAR_TABLE_FUNCTIONS and table.alias:
            # PostgreSQL treats a scalar function in FROM as a one-row table.
            alias = table.alias
            table.replace(
                exp.select(exp.alias_(source.copy(), alias)).subquery(alias)
            )
            return
        if name not in TABLE_FUNCTIONS and not isinstance(source, (exp.Unnest, exp.GenerateSeries)):
            raise SqlError(f"table function {name} is not allowed", "42501")
        table.set("db", None)
        table.set("catalog", None)
        return
    if not isinstance(source, exp.Identifier):
        raise SqlError("unsupported table expression", "42501")

    name = source.name
    schema = table.db
    catalog_name = table.catalog
    if catalog_name and catalog_name.lower() != context.database.lower():
        raise SqlError(f'cross-database reference "{catalog_name}" is not allowed', "42501")
    table.set("catalog", None)

    lowered = name.lower()
    if not schema:
        if lowered in ctes:
            return
        if lowered in catalog.PG_NAMES:
            schema = "pg_catalog"
        else:
            homes = context.tables.get(name) or context.tables.get(lowered) or []
            if len(homes) == 1:
                table.set("db", exp.to_identifier(homes[0], quoted=True))
                return
            if len(homes) > 1:
                raise SqlError(f'table name "{name}" is ambiguous, use source.table', "42P09")
            raise SqlError(f'relation "{name}" does not exist', "42P01")

    if schema.lower() == "pg_catalog":
        if lowered not in catalog.PG_NAMES:
            raise SqlError(f'relation "pg_catalog.{name}" is not available', "42P01")
        table.set("db", exp.to_identifier(context.catalog_schema, quoted=True))
        table.set("this", exp.to_identifier(lowered, quoted=True))
        _keep_name(table, name)
        return
    if schema.lower() == "information_schema":
        if lowered not in catalog.IS_NAMES:
            raise SqlError(f'relation "information_schema.{name}" is not available', "42P01")
        table.set("db", exp.to_identifier(context.catalog_schema, quoted=True))
        table.set("this", exp.to_identifier("is_" + lowered, quoted=True))
        _keep_name(table, name)
        return
    if schema.lower() not in context.allowed:
        raise SqlError(f'permission denied or unknown schema "{schema}"', "42501")


def _keep_name(table: exp.Table, name: str) -> None:
    """Keep references such as pg_class.oid working after the table moved."""
    if not table.alias:
        table.set("alias", exp.TableAlias(this=exp.to_identifier(name)))


def check_functions(tree: exp.Expression) -> None:
    """Refuse DuckDB machinery anywhere in the statement."""
    for node in tree.find_all(exp.Func):
        name = _function_name(node)
        if name and DENIED_FUNCTION.match(name):
            raise SqlError(f"function {name} is not allowed", "42501")


def generate(tree: exp.Expression, params: dict[int, exp.Expression] | None = None) -> str:
    """Render DuckDB SQL, with bound parameters inlined as literals."""
    check_functions(tree)
    if params is not None or tree.find(exp.Parameter):
        tree = tree.copy()
        bound = params or {}

        def replace(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Parameter):
                return bound.get(_param_number(node), exp.Null()).copy()
            return node

        tree = tree.transform(replace, copy=False)
    return tree.sql(dialect="duckdb")


def param_types(tree: exp.Expression, count: int) -> list[int]:
    """Guess parameter types from casts; PostgreSQL clients accept text otherwise."""
    result = [t.TEXT] * count
    for node in tree.find_all(exp.Cast):
        if isinstance(node.this, exp.Parameter):
            number = _param_number(node.this)
            if 0 < number <= count:
                result[number - 1] = _cast_oid(node.to)
    return result


def _cast_oid(kind: exp.DataType) -> int:
    name = kind.sql("postgres").lower()
    mapping = {
        "int": t.INT4, "integer": t.INT4, "bigint": t.INT8, "smallint": t.INT2, "boolean": t.BOOL,
        "double precision": t.FLOAT8, "real": t.FLOAT4, "date": t.DATE, "timestamp": t.TIMESTAMP,
        "timestamptz": t.TIMESTAMPTZ, "uuid": t.UUID,
    }
    return mapping.get(name, t.TEXT)
