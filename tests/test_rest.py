import pytest

from pgfederate import rest

from .conftest import Api


def no_sleep(_):
    pass


def test_flatten_nested_objects_and_lists():
    assert rest.flatten({"a": {"b": {"c": 1}}, "l": [1, 2], "x": None}) == {"a__b__c": 1, "l": "[1,2]", "x": None}


def test_flatten_respects_depth():
    assert rest.flatten({"a": {"b": {"c": 1}}}, depth=1) == {"a": '{"b":{"c":1}}'}


def test_flatten_scalar_record():
    assert rest.flatten(5) == {"value": 5}


@pytest.mark.parametrize(
    "path,expected", [("$", {"a": {"b": 1}}), ("", {"a": {"b": 1}}), ("a.b", 1), ("$.a.b", 1), ("a.c", None)]
)
def test_dig(path, expected):
    assert rest.dig({"a": {"b": 1}}, path) == expected


def test_link_header_pagination(api):
    table = {"path": "/issues", "pagination": {"type": "link"}}
    assert [r["id"] for r in rest.fetch_records(api, table, {}, no_sleep)] == [1, 2, 3, 4, 5, 6, 7]


def test_page_pagination_stops_on_empty_page(api):
    table = {"path": "/issues", "pagination": {"type": "page"}}
    assert len(list(rest.fetch_records(api, table, {}, no_sleep))) == 7


def test_max_pages_is_an_error_rather_than_a_silent_cut(api):
    table = {"path": "/issues", "pagination": {"type": "page", "max_pages": 2}}
    with pytest.raises(rest.RestError, match="max_pages"):
        list(rest.fetch_records(api, table, {}, no_sleep))


def test_records_path(api):
    table = {"path": "/wrapped", "records": "data.items"}
    assert len(list(rest.fetch_records(api, table, {}, no_sleep))) == 3


def test_missing_records_path(api):
    table = {"path": "/wrapped", "records": "data.nothing"}
    with pytest.raises(rest.RestError, match="no records"):
        list(rest.fetch_records(api, table, {}, no_sleep))


def test_retry_after_server_errors(api):
    Api.fail_next = 2
    table = {"path": "/wrapped", "records": "data.items"}
    assert len(list(rest.fetch_records(api, table, {}, no_sleep))) == 3


def test_gives_up_after_retries(api):
    Api.fail_next = rest.MAX_RETRIES + 1
    with pytest.raises(rest.RestError, match="gave up"):
        list(rest.fetch_records(api, {"path": "/wrapped", "records": "data.items"}, {}, no_sleep))
    Api.fail_next = 0


def test_client_error_is_not_retried(api):
    with pytest.raises(rest.RestError, match="HTTP 404"):
        list(rest.fetch_records(api, {"path": "/missing"}, {}, no_sleep))


def test_unknown_pagination(api):
    with pytest.raises(rest.RestError, match="unknown pagination"):
        list(rest.fetch_records(api, {"path": "/issues", "pagination": {"type": "magic"}}, {}, no_sleep))


def test_with_params_keeps_existing_query():
    assert rest.with_params("http://x/y?a=1", {"b": 2}) == "http://x/y?a=1&b=2"
