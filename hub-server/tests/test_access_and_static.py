import logging

import pytest
from conftest import ORIGIN, connect_daemon, connect_ui
from starlette.websockets import WebSocketDisconnect

LOGIN = "Tailscale-User-Login"


def test_allowlist_admits_listed_logins_only(make_client):
    client = make_client(allowed_logins=["daniel@example.com"])
    assert client.get("/api/boards").status_code == 403
    assert (
        client.get("/api/boards", headers={LOGIN: "eve@example.com"}).status_code == 403
    )
    allowed = client.get("/api/boards", headers={LOGIN: "daniel@example.com"})
    assert allowed.status_code == 200

    with pytest.raises(WebSocketDisconnect), connect_ui(client):
        pass
    with connect_ui(client, {LOGIN: "daniel@example.com"}):
        pass


def test_allowlist_does_not_apply_to_daemons(make_client, add_token):
    client = make_client(allowed_logins=["daniel@example.com"])
    with connect_daemon(client, add_token("dev-daniel")) as (_, reply):
        assert reply["type"] == "welcome"


def test_no_allowlist_admits_everyone_on_the_tailnet(client):
    assert client.get("/api/boards").status_code == 200


def test_serves_the_built_web_ui(make_client, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>tk-hub</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    client = make_client(web_dist=dist)

    assert "<title>tk-hub</title>" in client.get("/").text
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/api/boards").json() == {"boards": [], "devboxes": []}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "https://evil.example"}, {"Origin": "http://testserver.evil"}],
    ids=["no-origin", "foreign", "lookalike"],
)
def test_ui_socket_refuses_other_origins(client, headers):
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/ws/ui", headers=headers),
    ):
        pass
    assert refused.value.code == 1008


def test_ui_socket_accepts_the_proxied_origin(client):
    # Behind `tailscale serve` the hub sees plain http on its local address;
    # X-Forwarded-Proto/-Host carry the origin the browser actually used.
    proxied = {
        "Origin": "https://tk-hub.example.ts.net",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "tk-hub.example.ts.net",
    }
    with client.websocket_connect("/ws/ui", headers=proxied):
        pass


def test_a_bodyless_post_from_a_non_browser_client_works(client):
    # curl -X POST .../api/scan: no body, no Content-Type, no Origin.
    assert client.post("/api/scan").status_code == 202


def test_a_non_json_body_is_415(client):
    form = client.post(
        "/api/scan",
        content="devbox=dev-daniel",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415
    assert client.post("/api/scan", content="{}").status_code == 415


def test_state_changing_requests_refuse_other_origins(client):
    foreign = client.post(
        "/api/scan", json={}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    cross_site = client.post(
        "/api/scan", json={}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert cross_site.status_code == 403
    same = client.post("/api/scan", json={}, headers={"Origin": ORIGIN})
    assert same.status_code == 202


def test_ui_socket_accepts_its_own_origin_with_a_port(client):
    same = {"Host": "localhost:8000", "Origin": "http://localhost:8000"}
    with client.websocket_connect("/ws/ui", headers=same):
        pass
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/ws/ui", headers=same | {"Origin": "http://localhost:9999"}
        ),
    ):
        pass


def test_requests_for_other_host_names_are_refused(make_client, add_token):
    # DNS rebinding: a page on evil.example re-pointed at the hub's address.
    client = make_client(host_names=["tk-hub.example.ts.net"])
    evil = {"Host": "evil.example"}
    assert client.get("/api/boards", headers=evil).status_code == 400
    for path in ("/ws/ui", "/ws/daemon"):
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(
                path, headers=evil | {"Origin": "http://evil.example"}
            ),
        ):
            pass
        assert refused.value.code == 1008

    hub_name = {"Host": "tk-hub.example.ts.net"}
    assert client.get("/api/boards", headers=hub_name).status_code == 200
    for local in ("localhost:8000", "127.0.0.1", "testserver"):
        assert client.get("/api/boards", headers={"Host": local}).status_code == 200


def test_unknown_paths_fall_back_to_the_web_app(make_client, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>tk-hub</title>")
    client = make_client(web_dist=dist)

    assert "<title>tk-hub</title>" in client.get("/boards/dev-daniel/x").text
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/api/nothing").status_code == 404
    assert client.get("/ws/nothing").status_code == 404


def test_without_host_names_the_hub_warns_it_is_local_only(make_client, caplog):
    with caplog.at_level(logging.INFO, logger="hub_server"):
        make_client()
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "TK_HUB_HOSTNAMES" in warning.getMessage()
    assert "localhost" in warning.getMessage()


def test_configured_host_names_are_logged(make_client, caplog):
    with caplog.at_level(logging.INFO, logger="hub_server"):
        make_client(host_names=["tk-hub.example.ts.net"])
    messages = [r.getMessage() for r in caplog.records]
    assert any("tk-hub.example.ts.net" in m for m in messages)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
