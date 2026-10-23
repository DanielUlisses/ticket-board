from conftest import assert_valid, connect_daemon, connect_ui, fixture


def test_daemon_with_its_token_is_welcomed(client, add_token):
    token = add_token("dev-daniel")
    with connect_daemon(client, token) as (_, reply):
        assert reply == {"type": "welcome", "protocol_version": "1.0"}


def test_bad_token_is_refused_with_a_reason(client, add_token):
    add_token("dev-daniel")
    with connect_daemon(client, "not-the-token") as (_, reply):
        assert_valid(reply)
        assert reply["type"] == "refused"
        assert "token" in reply["reason"]


def test_major_version_mismatch_is_refused_and_the_ui_is_told_why(client, add_token):
    token = add_token("dev-daniel")
    hello = fixture("hello.json") | {"protocol_version": "2.0"}
    with connect_ui(client) as ui:
        with connect_daemon(client, token, hello) as (_, reply):
            assert_valid(reply)
            assert reply["type"] == "refused"
            assert "major" in reply["reason"]
        status = ui.receive_json()
    assert_valid(status)
    assert status["type"] == "devbox_status"
    assert status["devbox"] == "dev-daniel"
    assert status["online"] is False
    assert status["protocol_version"] == "2.0"
    assert status["refused_reason"] == reply["reason"]


def test_minor_version_difference_is_welcomed(client, add_token):
    token = add_token("dev-daniel")
    hello = fixture("hello.json") | {"protocol_version": "1.7"}
    with connect_daemon(client, token, hello) as (_, reply):
        assert reply["type"] == "welcome"
