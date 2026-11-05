import pytest

from pgfederate import config as cfg

SOURCE = {"name": "a", "type": "sqlite", "path": "x.db"}


def test_minimal():
    config = cfg.parse({"sources": [SOURCE]})
    assert config.port == 5433 and config.host == "127.0.0.1"
    assert config.sources[0].options == {"path": "x.db"}


def test_environment_interpolation(monkeypatch):
    monkeypatch.setenv("PGF_TEST_DSN", "host=db")
    config = cfg.parse(
        {"sources": [{"name": "a", "type": "postgres", "dsn": "${PGF_TEST_DSN} password=${MISSING_ONE:-none}"}]}
    )
    assert config.sources[0].options["dsn"] == "host=db password=none"


def test_missing_variable():
    with pytest.raises(cfg.ConfigError, match="NOT_SET_ANYWHERE"):
        cfg.parse({"sources": [{"name": "a", "type": "postgres", "dsn": "${NOT_SET_ANYWHERE}"}]})


@pytest.mark.parametrize(
    "value,seconds", [("30s", 30), ("15m", 900), ("2h", 7200), ("1d", 86400), (45, 45), (None, None)]
)
def test_duration(value, seconds):
    assert cfg.parse_duration(value) == seconds


def test_bad_duration():
    with pytest.raises(cfg.ConfigError):
        cfg.parse_duration("soon")


@pytest.mark.parametrize(
    "data,message",
    [
        ({}, "at least one source"),
        ({"sources": [{"name": "Bad-Name", "type": "sqlite"}]}, "must match"),
        ({"sources": [{"name": "pg_x", "type": "sqlite"}]}, "reserved"),
        ({"sources": [{"name": "main", "type": "sqlite"}]}, "reserved"),
        ({"sources": [{"name": "a", "type": "oracle"}]}, "type must be"),
        ({"sources": [SOURCE, SOURCE]}, "duplicate"),
        ({"sources": [SOURCE], "nope": 1}, "unknown top-level"),
        ({"sources": [SOURCE], "users": [{"name": "x"}]}, "password"),
        ({"sources": [SOURCE], "users": [{"name": "x", "password": "y", "sources": ["b"]}]}, "unknown source"),
        ({"sources": [SOURCE], "relations": [{"from": "a.t.c"}]}, "from and to"),
        ({"sources": [SOURCE], "relations": [{"from": "a.t", "to": "a.u.c"}]}, "source.table.column"),
        ({"sources": [SOURCE], "relations": [{"from": "a.t.c", "to": "b.u.c"}]}, "unknown source"),
        ({"sources": [SOURCE], "primary_keys": {"b.t": "id"}}, "source.table"),
        ({"sources": [SOURCE], "server": {"tls": {"cert": "c"}}}, "both cert and key"),
    ],
)
def test_validation(data, message):
    with pytest.raises(cfg.ConfigError, match=message):
        cfg.parse(data)


def test_relations_and_primary_keys():
    config = cfg.parse(
        {
            "sources": [SOURCE, {"name": "b", "type": "rest"}],
            "relations": [{"from": "a.t.c", "to": "b.u.d"}],
            "primary_keys": {"b.u": "d"},
        }
    )
    assert config.relations[0].to_table == "u"
    assert config.primary_keys[("b", "u")] == ["d"]
