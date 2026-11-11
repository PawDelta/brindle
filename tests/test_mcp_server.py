import anyio
import mcp_types as types
import pytest
from mcp.shared.message import SessionMessage

from brindle import config, mcp_server, upgrade


class _FakeConnection:
    """What ``mcp_server._serve`` drives of an mcp_stdio.Connection: the MCP
    server runs on memory streams the test is the client of."""

    def __init__(self):
        self.to_server, self._read = anyio.create_memory_object_stream(16)
        self._write, self.from_server = anyio.create_memory_object_stream(16)

    def wake(self) -> None:
        pass

    async def serve(self, run) -> None:
        await run(self._read, self._write)

    async def send(self, **message) -> None:
        await self.to_server.send(SessionMessage(types.jsonrpc_message_adapter.validate_python(
            {"jsonrpc": "2.0", **message})))

    async def answer(self, request_id) -> dict:
        while True:
            message = (await self.from_server.receive()).message.model_dump(by_alias=True, exclude_none=True)
            if message.get("id") == request_id:
                return message

    def notices(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.from_server.receive_nowait().message.method)
            except anyio.WouldBlock:
                return out


@pytest.mark.parametrize("trouble", ["config.json mid-write", "notice raised"])
def test_a_tools_changed_watch_that_fails_never_stops_the_server(monkeypatch, trouble):
    """0.6.3's visuals watch shares _serve's task group with the connection:
    a look at the setting that raised (config.json caught mid-write, as a
    truncate-then-write left it for a reader), or a
    tools/list_changed that raised, cancelled the connection, so the next
    tools/call was never answered. Now the call is answered, and a notice
    that failed is sent on the next look."""
    monkeypatch.setattr(upgrade, "install", type("Install", (), {"changed": staticmethod(lambda: False)}))
    monkeypatch.setattr(upgrade, "stamp", lambda: "same")
    monkeypatch.delenv("BRINDLE_VISUALS", raising=False)
    monkeypatch.setattr(mcp_server.mcp, "client_session", None, raising=False)
    real_notify, failed = mcp_server.mcp.notify_tools_changed, []

    async def notify():
        if not failed:
            failed.append(1)
            raise RuntimeError("the SDK's send failed")
        return await real_notify()

    monkeypatch.setattr(mcp_server.mcp, "notify_tools_changed", notify)
    pending: list[anyio.Event] = []

    async def wait(seconds):    # the fake clock: time passes only at tick()
        pending.append(event := anyio.Event())
        await event.wait()

    async def tick():
        await anyio.wait_all_tasks_blocked()
        while pending:
            pending.pop().set()
        await anyio.wait_all_tasks_blocked()

    conn = _FakeConnection()

    async def client():
        await conn.send(id=0, method="initialize", params={
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        await conn.answer(0)
        await conn.send(method="notifications/initialized")
        await conn.send(id=1, method="tools/list")
        await conn.answer(1)
        if trouble == "config.json mid-write":
            config.user_config_path().parent.mkdir(parents=True, exist_ok=True)
            config.user_config_path().write_text('{\n  "visuals_enabled": "o')    # cut off mid-write
            await tick()
        else:
            config.set_user("visuals_enabled", "off")
            await tick()        # the look sees the flip; its notice raises
            await tick()        # the next look sends it again
            assert "notifications/tools/list_changed" in conn.notices()
        await conn.send(id=2, method="tools/call", params={"name": "no_such_tool", "arguments": {}})
        with anyio.fail_after(10):      # reached only when the call isn't answered
            assert "result" in await conn.answer(2)
        conn.to_server.close()

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(client)
            await mcp_server._serve(conn, wait=wait)

    anyio.run(scenario)
