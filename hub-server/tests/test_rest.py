from conftest import board, connect_daemon, connect_ui, read_until, snapshot, ticket

KEY = "dev-daniel/ticket-skill/tk-hub"


def sync(client, token, boards=None):
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        daemon.send_json(snapshot(boards=boards))
        read_until(ui, "devbox_status")
        read_until(ui, "devbox_status")


def test_report_returns_the_full_report_and_the_raw_ticket_body(client, add_token):
    with_body = board(KEY)
    ticket(with_body, "01")["body"] = "# 01: tk.sh boards\n\n**What to build:** ..."
    sync(client, add_token("dev-daniel"), boards=[with_body])

    report = client.get(f"/api/boards/{KEY}/tickets/01/report")
    assert report.status_code == 200
    assert report.json() == {
        "board_key": KEY,
        "nn": "01",
        "title": "tk.sh boards + tk.sh export as JSON",
        "full_report_md": ticket(board(KEY), "01")["full_report_md"],
        "body": "# 01: tk.sh boards\n\n**What to build:** ...",
    }


def test_report_body_is_null_when_the_daemon_sends_none(client, add_token):
    sync(client, add_token("dev-daniel"))
    report = client.get(f"/api/boards/{KEY}/tickets/02/report").json()
    assert report["body"] is None
    assert report["full_report_md"] == ticket(board(KEY), "02")["full_report_md"]


def test_unknown_board_or_ticket_is_404(client, add_token):
    sync(client, add_token("dev-daniel"))
    assert client.get(f"/api/boards/{KEY}/tickets/99/report").status_code == 404
    assert client.get("/api/boards/a/b/c/tickets/01/report").status_code == 404
    assert client.get("/api/boards/a/b/c/tickets/01/activity").status_code == 404
