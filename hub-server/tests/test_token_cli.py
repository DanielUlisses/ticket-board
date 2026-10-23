import sqlite3

from conftest import connect_daemon

from hub_server.cli import main as cli


def test_token_add_prints_the_token_once_and_stores_only_a_hash(db, capsys):
    assert cli(["token", "add", "dev-daniel", "--db", str(db)]) == 0
    token = capsys.readouterr().out.strip()
    assert len(token) >= 40

    dump = "\n".join(sqlite3.connect(db).iterdump())
    assert token not in dump
    assert "dev-daniel" in dump


def test_token_add_twice_is_refused(db, add_token, capsys):
    add_token("dev-daniel")
    assert cli(["token", "add", "dev-daniel", "--db", str(db)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "revoke" in captured.err


def test_revoked_token_is_refused(client, db, add_token):
    token = add_token("dev-daniel")
    assert cli(["token", "revoke", "dev-daniel", "--db", str(db)]) == 0
    with connect_daemon(client, token) as (_, reply):
        assert reply["type"] == "refused"


def test_revoke_of_an_unknown_devbox_fails(db):
    assert cli(["token", "revoke", "dev-nobody", "--db", str(db)]) == 1


def test_db_defaults_to_tk_hub_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TK_HUB_DB", str(tmp_path / "env.db"))
    assert cli(["token", "add", "dev-daniel"]) == 0
    assert (tmp_path / "env.db").exists()
