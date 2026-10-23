"""Where the hub finds protocol/schema.json at runtime."""

import pytest

from hub_server import protocol


def test_tk_hub_schema_overrides(tmp_path, monkeypatch):
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    monkeypatch.setenv("TK_HUB_SCHEMA", str(schema))
    assert protocol.find_schema() == schema


def test_a_missing_schema_names_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("TK_HUB_SCHEMA", str(tmp_path / "nowhere.json"))
    with pytest.raises(RuntimeError, match="TK_HUB_SCHEMA"):
        protocol.find_schema()


def test_without_the_variable_the_shipped_or_repo_schema_is_found(monkeypatch):
    monkeypatch.delenv("TK_HUB_SCHEMA", raising=False)
    assert protocol.find_schema().name == "schema.json"
