"""Fetch JSON records from an HTTP API, following its pagination."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

MAX_RETRIES = 4
LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


class RestError(RuntimeError):
    """An API request failed for good."""


def dig(document: Any, path: str | None) -> Any:
    """Follow a dotted path such as data.items; '' or '$' is the document."""
    if path in (None, "", "$"):
        return document
    for key in path.removeprefix("$.").split("."):
        if not isinstance(document, dict) or key not in document:
            return None
        document = document[key]
    return document


def flatten(record: Any, depth: int = 3) -> dict[str, Any]:
    """Turn nested objects into a__b columns; lists become JSON text."""
    if not isinstance(record, dict):
        return {"value": record}
    flat: dict[str, Any] = {}

    def walk(prefix: str, value: dict[str, Any], level: int) -> None:
        for key, item in value.items():
            name = f"{prefix}{key}"
            if isinstance(item, dict) and level < depth:
                walk(f"{name}__", item, level + 1)
            elif isinstance(item, (dict, list)):
                flat[name] = json.dumps(item, separators=(",", ":"))
            else:
                flat[name] = item

    walk("", record, 1)
    return flat


def request(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    body: Any = None,
    timeout: float = 30,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Any, dict[str, str]]:
    """Return decoded JSON and headers; retry 429, 5xx and network errors."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "pgfederate", **headers}
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        wait = 2.0**attempt
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response), {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise RestError(f"{method} {url}: HTTP {error.code}") from error
            retry_after = error.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else wait
            last = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last = str(error)
        except json.JSONDecodeError as error:
            raise RestError(f"{method} {url}: response is not JSON") from error
        if attempt < MAX_RETRIES:
            sleep(min(wait, 60))
    raise RestError(f"{method} {url}: gave up after {MAX_RETRIES + 1} attempts ({last})")


def with_params(url: str, params: dict[str, Any]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items()})
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def fetch_records(
    base_url: str,
    table: dict[str, Any],
    headers: dict[str, str],
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict[str, Any]]:
    """Yield flattened records of one configured REST table."""
    path = table.get("path", "")
    url = path if path.startswith("http") else base_url.rstrip("/") + "/" + path.lstrip("/")
    params = dict(table.get("params") or {})
    method = table.get("method", "GET").upper()
    body = table.get("body")
    records_path = table.get("records")
    pagination = dict(table.get("pagination") or {})
    style = pagination.get("type", "none")
    max_pages = int(pagination.get("max_pages", 1000))
    depth = int(table.get("flatten_depth", 3))
    timeout = float(table.get("timeout", 30))

    if style not in {"none", "link", "page", "offset", "cursor", "next_url"}:
        raise RestError(f"unknown pagination type {style!r}")

    page = int(pagination.get("start", 1))
    offset = 0
    limit = int(pagination.get("limit", 100))
    if style == "offset":
        params[pagination.get("limit_param", "limit")] = limit
    next_url: str | None = None

    for _ in range(max_pages):
        if style == "page":
            params[pagination.get("param", "page")] = page
        elif style == "offset":
            params[pagination.get("param", "offset")] = offset
        target = next_url or with_params(url, params)
        document, response_headers = request(target, headers, method, body, timeout, sleep)
        records = dig(document, records_path)
        if records is None:
            raise RestError(f"{target}: no records at {records_path!r}")
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise RestError(f"{target}: records at {records_path!r} are not a list")
        for record in records:
            yield flatten(record, depth)

        if style == "none":
            return
        if style == "link":
            match = LINK_NEXT.search(response_headers.get("link", ""))
            if not match:
                return
            next_url = match.group(1)
        elif style == "next_url":
            value = dig(document, pagination.get("path", "next"))
            if not value:
                return
            next_url = urllib.parse.urljoin(target, value)
        elif style == "cursor":
            value = dig(document, pagination.get("path", "next_cursor"))
            if not value:
                return
            params[pagination.get("param", "cursor")] = value
        elif style == "page":
            if not records:
                return
            page += 1
        elif style == "offset":
            if len(records) < limit:
                return
            offset += limit
    raise RestError(f"{url}: stopped after max_pages={max_pages}")
