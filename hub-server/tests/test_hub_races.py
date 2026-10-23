"""Interleavings TestClient cannot produce on demand, driven on Hub directly with
fake sockets: a reconnect that lands while the old connection is being cleaned
up, a socket replaced under pending rpcs, a box dropping out during scan-all."""

import asyncio

from conftest import fixture, snapshot

from hub_server.hub import Hub


class FakeSocket:
    """Records what the hub sends; `on_send` runs once, mid-send."""

    def __init__(self, on_send=None):
        self.sent = []
        self.closed = False
        self.on_send = on_send

    async def send_json(self, message):
        self.sent.append(message)
        if self.on_send:
            hook, self.on_send = self.on_send, None
            await hook(message)

    async def close(self, code=1000):
        self.closed = True


def hello(devbox="dev-daniel"):
    return fixture("hello.json") | {"devbox": devbox}


def results(ui):
    return {m["id"]: m for m in ui.sent if m["type"] == "rpc_result"}


def test_a_reconnect_during_disconnect_cleanup_stays_online(tmp_path):
    async def scenario():
        hub = Hub(tmp_path / "hub.db")
        old, new = FakeSocket(), FakeSocket()
        await hub.connected("dev-daniel", old, hello())
        await hub.apply_snapshot("dev-daniel", snapshot())
        old_rpc = await hub.send_rpc("dev-daniel", "rescan_boards", {})

        async def reconnect(_message):
            await hub.connected("dev-daniel", new, hello())
            await hub.apply_snapshot("dev-daniel", snapshot())
            new_rpc.append(await hub.send_rpc("dev-daniel", "rescan_boards", {}))

        new_rpc = []
        ui = FakeSocket(on_send=reconnect)
        hub.ui.add(ui)
        await hub.disconnected("dev-daniel", old)
        return hub, ui, old_rpc, new_rpc[0]

    hub, ui, old_rpc, new_rpc = asyncio.run(scenario())
    assert hub.store.devbox_status("dev-daniel")["online"] is True
    assert not any(b["stale"] for b in hub.store.boards.all())
    assert results(ui)[old_rpc]["reason"] == "devbox disconnected"
    assert new_rpc not in results(ui)
    assert new_rpc in hub.pending


def test_a_replaced_socket_fails_its_own_pending_rpcs(tmp_path):
    async def scenario():
        hub = Hub(tmp_path / "hub.db")
        ui = FakeSocket()
        hub.ui.add(ui)
        old, new = FakeSocket(), FakeSocket()
        await hub.connected("dev-daniel", old, hello())
        rpc = await hub.send_rpc("dev-daniel", "rescan_boards", {})
        await hub.connected("dev-daniel", new, hello())
        return hub, ui, old, rpc

    hub, ui, old, rpc = asyncio.run(scenario())
    assert old.closed
    assert results(ui)[rpc]["ok"] is False
    assert rpc not in hub.pending
    assert hub.store.devbox_status("dev-daniel")["online"] is True


def test_scan_all_skips_a_box_that_drops_out_mid_loop(tmp_path):
    async def scenario():
        hub = Hub(tmp_path / "hub.db")
        acme = FakeSocket()

        async def drop_daniel(_message):
            await hub.disconnected("dev-daniel", daniel)

        # sorted: dev-acme is sent first, and dev-daniel drops while it is.
        acme.on_send = drop_daniel
        daniel = FakeSocket()
        await hub.connected("dev-acme", acme, hello("dev-acme"))
        await hub.connected("dev-daniel", daniel, hello())
        return await hub.scan()

    sent = asyncio.run(scenario())
    assert [r["devbox"] for r in sent] == ["dev-acme"]


def test_the_ui_ends_on_the_reconnected_state(tmp_path):
    """The cleanup of the old connection must not broadcast its stale/offline
    view after the new connection's snapshot."""

    async def scenario():
        hub = Hub(tmp_path / "hub.db")
        old, new = FakeSocket(), FakeSocket()
        await hub.connected("dev-daniel", old, hello())
        await hub.apply_snapshot("dev-daniel", snapshot())

        async def reconnect(_message):
            await hub.connected("dev-daniel", new, hello())
            await hub.apply_snapshot("dev-daniel", snapshot())

        ui = FakeSocket(on_send=reconnect)
        hub.ui.add(ui)
        await hub.disconnected("dev-daniel", old)
        return ui

    ui = asyncio.run(scenario())
    statuses = [m for m in ui.sent if m["type"] == "devbox_status"]
    assert statuses[-1]["online"] is True
    stale = [m["board"]["stale"] for m in ui.sent if m["type"] == "board_synced"]
    # The old connection's cleanup may get one stale board out before the
    # reconnect lands; none may follow the new snapshot's fresh boards.
    assert False in stale
    first_fresh = stale.index(False)
    assert not any(stale[first_fresh:])


def test_a_disconnect_during_a_snapshots_broadcast_wins(tmp_path):
    async def scenario():
        hub = Hub(tmp_path / "hub.db")
        daemon = FakeSocket()
        await hub.connected("dev-daniel", daemon, hello())

        async def drop(_message):
            await hub.disconnected("dev-daniel", daemon)

        ui = FakeSocket(on_send=drop)
        hub.ui.add(ui)
        await hub.apply_snapshot("dev-daniel", snapshot())
        return ui

    ui = asyncio.run(scenario())
    statuses = [m for m in ui.sent if m["type"] == "devbox_status"]
    assert statuses[-1]["online"] is False
    synced = [m["board"] for m in ui.sent if m["type"] == "board_synced"]
    last = {b["key"]: b["stale"] for b in synced}  # the UI keeps the last one
    assert last and all(last.values())


def test_rpc_results_count_only_from_the_socket_they_were_sent_on(tmp_path):
    async def scenario():
        hub = Hub(tmp_path / "hub.db")
        ui = FakeSocket()
        hub.ui.add(ui)
        old, new = FakeSocket(), FakeSocket()
        await hub.connected("dev-daniel", old, hello())
        on_old = await hub.send_rpc("dev-daniel", "rescan_boards", {})
        await hub.connected("dev-daniel", new, hello())
        on_new = await hub.send_rpc("dev-daniel", "rescan_boards", {})
        ok = fixture("rpc-result-ok.json")
        # The replaced socket's late answers, to its own rpc and to the new one.
        await hub.received("dev-daniel", old, ok | {"id": on_old})
        await hub.received("dev-daniel", old, ok | {"id": on_new})
        # The new socket answering an rpc that was sent on the old one.
        await hub.received("dev-daniel", new, ok | {"id": on_old})
        # Garbage ids don't tear the connection down.
        await hub.received("dev-daniel", new, ok | {"id": ["r-1"]})
        await hub.received("dev-daniel", new, ok | {"id": {"x": 1}})
        return hub, ui, on_old, on_new

    hub, ui, on_old, on_new = asyncio.run(scenario())
    answers = [m for m in ui.sent if m["type"] == "rpc_result"]
    assert [(m["id"], m["ok"]) for m in answers] == [(on_old, False)]
    assert answers[0]["reason"] == "devbox reconnected"
    assert on_new in hub.pending


def test_a_version_refused_second_connection_leaves_the_live_one_online(tmp_path):
    async def scenario():
        hub = Hub(tmp_path / "hub.db")
        token = hub.store.add_token("dev-daniel")
        await hub.connected("dev-daniel", FakeSocket(), hello())
        ui = FakeSocket()
        hub.ui.add(ui)
        reason = await hub.admit(hello() | {"protocol_version": "2.0"}, token)
        return hub, ui, reason

    hub, ui, reason = asyncio.run(scenario())
    assert "major" in reason
    assert ui.sent == []
    assert hub.store.devbox_status("dev-daniel")["online"] is True


def test_a_board_back_before_its_removal_is_sent_is_not_removed(tmp_path):
    async def scenario():
        hub = Hub(tmp_path / "hub.db")
        await hub.connected("dev-daniel", FakeSocket(), hello())
        full = snapshot()
        await hub.apply_snapshot("dev-daniel", full)

        async def boards_come_back(_message):
            await hub.apply_snapshot("dev-daniel", full)

        ui = FakeSocket(on_send=boards_come_back)
        hub.ui.add(ui)
        await hub.apply_snapshot("dev-daniel", snapshot(boards=full["boards"][:1]))
        return hub, ui

    hub, ui = asyncio.run(scenario())
    assert len(hub.store.boards.all()) == 3
    assert [m for m in ui.sent if m["type"] == "board_removed"] == []


def test_a_stalled_ui_is_dropped_without_blocking_the_hub(tmp_path):
    class Stalled(FakeSocket):
        async def send_json(self, message):
            await asyncio.sleep(3600)

    async def scenario():
        hub = Hub(tmp_path / "hub.db", ui_send_timeout=0.05)
        stalled, live = Stalled(), FakeSocket()
        hub.ui.update({stalled, live})
        await asyncio.wait_for(
            hub.broadcast({"type": "board_removed", "board_key": "a/b/c"}), 1
        )
        await asyncio.sleep(0)  # let the spawned close run
        return hub, stalled, live

    hub, stalled, live = asyncio.run(scenario())
    assert hub.ui == {live}
    assert stalled.closed
    assert live.sent == [{"type": "board_removed", "board_key": "a/b/c"}]
