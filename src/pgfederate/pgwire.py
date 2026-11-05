"""PostgreSQL protocol 3 messages and value encoding."""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import hashlib
import hmac
import json
import math
import os
import re
import struct
import uuid
from typing import Any

from sqlglot import exp

from . import types as t

PG_EPOCH = dt.datetime(2000, 1, 1)
PG_EPOCH_DATE = dt.date(2000, 1, 1)
PG_EPOCH_UTC = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


def message(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def cstring(value: str) -> bytes:
    return value.encode() + b"\0"


def row_description(columns: list[tuple[str, int]], formats: list[int] | None = None) -> bytes:
    payload = struct.pack("!H", len(columns))
    for index, (name, oid) in enumerate(columns):
        code = formats[index] if formats else 0
        payload += cstring(name) + struct.pack("!IHIhih", 0, 0, oid, t.typlen(oid), -1, code)
    return message(b"T", payload)


def error_response(text: str, code: str = "XX000", severity: str = "ERROR") -> bytes:
    payload = b"S" + cstring(severity) + b"V" + cstring(severity) + b"C" + cstring(code) + b"M" + cstring(text) + b"\0"
    return message(b"E", payload)


def parameter_status(name: str, value: str) -> bytes:
    return message(b"S", cstring(name) + cstring(value))


def command_complete(tag: str) -> bytes:
    return message(b"C", cstring(tag))


def ready(status: bytes) -> bytes:
    return message(b"Z", status)


# values to the client ---------------------------------------------------------


def _json_default(value: Any) -> str:
    return encode_text(value) if not isinstance(value, (bytes, bytearray)) else value.hex()


def _interval(value: dt.timedelta) -> str:
    days = value.days
    seconds = value.seconds
    text = f"{days} days " if days else ""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    fraction = f".{value.microseconds:06d}".rstrip("0").rstrip(".") if value.microseconds else ""
    return f"{text}{hours:02d}:{minutes:02d}:{secs:02d}{fraction}"


def encode_text(value: Any) -> str:
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return repr(value)
    if isinstance(value, (int, decimal.Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return _interval(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, default=_json_default)
    return str(value)


def _numeric_binary(value: Any) -> bytes:
    number = decimal.Decimal(value)
    if number.is_nan():
        return struct.pack("!HhHH", 0, 0, 0xC000, 0)
    sign, digits, exponent = number.as_tuple()
    scale = max(-exponent, 0)
    text = "".join(map(str, digits)) + "0" * max(exponent, 0)
    integer = text[: len(text) - scale] if scale else text
    fraction = text[len(text) - scale :] if scale else ""
    if len(text) < scale:
        integer, fraction = "", text.rjust(scale, "0")
    integer = integer.rjust((len(integer) + 3) // 4 * 4, "0")
    fraction = fraction.ljust((len(fraction) + 3) // 4 * 4, "0")
    groups = [int(integer[i : i + 4]) for i in range(0, len(integer), 4)]
    weight = len(groups) - 1
    groups += [int(fraction[i : i + 4]) for i in range(0, len(fraction), 4)]
    while groups and groups[0] == 0:
        groups.pop(0)
        weight -= 1
    while groups and groups[-1] == 0:
        groups.pop()
    if not groups:
        weight = 0
    header = struct.pack("!HhHH", len(groups), weight, 0x4000 if sign else 0, scale)
    return header + b"".join(struct.pack("!H", group) for group in groups)


def encode_binary(value: Any, oid: int) -> bytes:
    if oid == t.BOOL:
        return b"\x01" if value else b"\0"
    if oid == t.INT2:
        return struct.pack("!h", value)
    if oid == t.INT4:
        return struct.pack("!i", value)
    if oid == t.INT8:
        return struct.pack("!q", value)
    if oid == t.FLOAT4:
        return struct.pack("!f", value)
    if oid == t.FLOAT8:
        return struct.pack("!d", value)
    if oid == t.NUMERIC:
        return _numeric_binary(value)
    if oid == t.DATE:
        return struct.pack("!i", (value - PG_EPOCH_DATE).days)
    if oid == t.TIME:
        return struct.pack("!q", ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond)
    if oid == t.TIMESTAMP:
        delta = value - PG_EPOCH
        return struct.pack("!q", (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds)
    if oid == t.TIMESTAMPTZ:
        delta = value - PG_EPOCH_UTC
        return struct.pack("!q", (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds)
    if oid == t.INTERVAL:
        micros = value.seconds * 1_000_000 + value.microseconds
        return struct.pack("!qii", micros, value.days, 0)
    if oid == t.UUID:
        return (value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))).bytes
    if oid == t.BYTEA:
        return bytes(value)
    return encode_text(value).encode()


def encode_row(row: tuple[Any, ...], columns: list[tuple[str, int]], formats: list[int] | None) -> bytes:
    payload = bytearray(struct.pack("!H", len(row)))
    for index, value in enumerate(row):
        if value is None:
            payload += b"\xff\xff\xff\xff"
            continue
        if formats and formats[index] == 1:
            data = encode_binary(value, columns[index][1])
        else:
            data = encode_text(value).encode()
        payload += struct.pack("!I", len(data)) + data
    return message(b"D", bytes(payload))


# values from the client -----------------------------------------------------------


NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
NUMERIC_OIDS = {t.INT2, t.INT4, t.INT8, t.FLOAT4, t.FLOAT8, t.NUMERIC, t.OID}
TYPED_LITERALS = {
    t.DATE: "DATE",
    t.TIME: "TIME",
    t.TIMESTAMP: "TIMESTAMP",
    t.TIMESTAMPTZ: "TIMESTAMPTZ",
    t.UUID: "UUID",
}


def decode_binary(data: bytes, oid: int) -> Any:
    if oid == t.BOOL:
        return data != b"\0"
    if oid == t.INT2:
        return struct.unpack("!h", data)[0]
    if oid in (t.INT4, t.OID):
        return struct.unpack("!i", data)[0]
    if oid == t.INT8:
        return struct.unpack("!q", data)[0]
    if oid == t.FLOAT4:
        return struct.unpack("!f", data)[0]
    if oid == t.FLOAT8:
        return struct.unpack("!d", data)[0]
    if oid == t.DATE:
        return (PG_EPOCH_DATE + dt.timedelta(days=struct.unpack("!i", data)[0])).isoformat()
    if oid in (t.TIMESTAMP, t.TIMESTAMPTZ):
        moment = PG_EPOCH + dt.timedelta(microseconds=struct.unpack("!q", data)[0])
        return moment.isoformat(sep=" ") + ("+00" if oid == t.TIMESTAMPTZ else "")
    if oid == t.UUID:
        return str(uuid.UUID(bytes=data))
    if oid == t.BYTEA:
        return "\\x" + data.hex()
    return data.decode()


def parameter_literal(data: bytes | None, oid: int, code: int) -> exp.Expression:
    """Render one bound value as a SQL literal; quoting is sqlglot's job."""
    if data is None:
        return exp.Null()
    value = decode_binary(data, oid) if code == 1 else data.decode()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return exp.Cast(this=exp.Literal.string(str(value)), to=exp.DataType.build("DOUBLE"))
        return exp.Literal.number(value)
    if oid == t.BOOL:
        return exp.Boolean(this=value.lower() in {"t", "true", "1", "yes", "on"})
    if oid in NUMERIC_OIDS:
        if not NUMBER.match(value):
            raise ValueError(f"invalid numeric parameter {value!r}")
        return exp.Literal.number(value)
    if oid in TYPED_LITERALS:
        return exp.Cast(this=exp.Literal.string(value), to=exp.DataType.build(TYPED_LITERALS[oid], dialect="duckdb"))
    return exp.Literal.string(value)


# SCRAM-SHA-256 ------------------------------------------------------------------------


class Scram:
    """Server side of the SCRAM-SHA-256 exchange for one login."""

    iterations = 4096

    def __init__(self, password: str) -> None:
        self.salt = os.urandom(16)
        salted = hashlib.pbkdf2_hmac("sha256", password.encode(), self.salt, self.iterations)
        client_key = hmac.digest(salted, b"Client Key", "sha256")
        self.stored_key = hashlib.sha256(client_key).digest()
        self.server_key = hmac.digest(salted, b"Server Key", "sha256")
        self.nonce = ""
        self.client_first_bare = ""
        self.server_first = ""

    def first(self, client_first: str) -> str:
        self.client_first_bare = client_first.split(",", 2)[2]
        attributes = dict(item.split("=", 1) for item in self.client_first_bare.split(","))
        self.nonce = attributes["r"] + base64.b64encode(os.urandom(18)).decode()
        salt = base64.b64encode(self.salt).decode()
        self.server_first = f"r={self.nonce},s={salt},i={self.iterations}"
        return self.server_first

    def final(self, client_final: str) -> str | None:
        without_proof, proof = client_final.rsplit(",p=", 1)
        attributes = dict(item.split("=", 1) for item in without_proof.split(","))
        if attributes.get("r") != self.nonce:
            return None
        auth = f"{self.client_first_bare},{self.server_first},{without_proof}".encode()
        signature = hmac.digest(self.stored_key, auth, "sha256")
        claimed = base64.b64decode(proof)
        client_key = bytes(a ^ b for a, b in zip(claimed, signature))
        if not hmac.compare_digest(hashlib.sha256(client_key).digest(), self.stored_key):
            return None
        verifier = hmac.digest(self.server_key, auth, "sha256")
        return "v=" + base64.b64encode(verifier).decode()
