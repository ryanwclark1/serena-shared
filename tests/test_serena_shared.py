"""Unit tests for serena-shared proxy.py and reaper.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import (
    METHOD_NOT_FOUND,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import proxy  # noqa: E402
import reaper  # noqa: E402


class TestProxy(unittest.TestCase):
    def test_pre_init_request_replied_locally_and_notification_dropped(self) -> None:
        async def scenario() -> None:
            client_in_w, client_in_r = anyio.create_memory_object_stream(10)
            server_out_w, server_out_r = anyio.create_memory_object_stream(10)
            client_out_w, client_out_r = anyio.create_memory_object_stream(10)

            session_id: list[str | None] = [None]
            init_state = {"initialized": False, "init_id": None, "terminated": False}

            # Enqueue: server/discover (pre-init), notification (pre-init), initialize
            await client_in_w.send(
                SessionMessage(
                    JSONRPCMessage(
                        JSONRPCRequest(jsonrpc="2.0", id=1, method="server/discover", params={})
                    )
                )
            )
            await client_in_w.send(
                SessionMessage(
                    JSONRPCMessage(
                        JSONRPCNotification(
                            jsonrpc="2.0", method="notifications/roots/list_changed", params={}
                        )
                    )
                )
            )
            await client_in_w.send(
                SessionMessage(
                    JSONRPCMessage(
                        JSONRPCRequest(jsonrpc="2.0", id=2, method="initialize", params={})
                    )
                )
            )
            await client_in_w.aclose()

            with mock.patch("proxy.os._exit") as mock_exit, mock.patch("proxy.httpx.delete"):
                await proxy._pump_client_to_server(
                    client_in_r,
                    server_out_w,
                    client_out_w,
                    "http://127.0.0.1:29100/mcp",
                    lambda: session_id[0],
                    init_state,
                )
                mock_exit.assert_called_once_with(0)

            # Client should receive local -32601 error for server/discover
            reply = client_out_r.receive_nowait()
            self.assertIsInstance(reply.message.root, JSONRPCError)
            self.assertEqual(reply.message.root.id, 1)
            self.assertEqual(reply.message.root.error.code, METHOD_NOT_FOUND)

            # Server should ONLY receive the initialize request (notification was dropped)
            forwarded = server_out_r.receive_nowait()
            self.assertIsInstance(forwarded.message.root, JSONRPCRequest)
            self.assertEqual(forwarded.message.root.method, "initialize")
            self.assertEqual(init_state["init_id"], 2)
            with self.assertRaises(anyio.WouldBlock):
                server_out_r.receive_nowait()

            for stream in (client_in_r, server_out_w, server_out_r, client_out_w, client_out_r):
                await stream.aclose()

        anyio.run(scenario)

    def test_server_to_client_marks_initialized(self) -> None:
        async def scenario() -> None:
            server_in_w, server_in_r = anyio.create_memory_object_stream(10)
            client_out_w, client_out_r = anyio.create_memory_object_stream(10)
            init_state = {"initialized": False, "init_id": 2, "terminated": False}

            await server_in_w.send(
                SessionMessage(
                    JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=2, result={"ok": True}))
                )
            )
            await server_in_w.aclose()

            with mock.patch("proxy.os._exit"), mock.patch("proxy.httpx.delete"):
                await proxy._pump_server_to_client(
                    server_in_r,
                    client_out_w,
                    "http://127.0.0.1:29100/mcp",
                    lambda: "sess-123",
                    init_state,
                )

            self.assertTrue(init_state["initialized"])
            msg = client_out_r.receive_nowait()
            self.assertIsInstance(msg.message.root, JSONRPCResponse)
            self.assertEqual(msg.message.root.id, 2)

            for stream in (server_in_r, client_out_w, client_out_r):
                await stream.aclose()

        anyio.run(scenario)

    def test_terminate_session_fires_once(self) -> None:
        init_state = {"terminated": False}
        with mock.patch("proxy.httpx.delete") as mock_delete:
            proxy._terminate_session("http://127.0.0.1:29100/mcp", "abc123", init_state)
            proxy._terminate_session("http://127.0.0.1:29100/mcp", "abc123", init_state)
            mock_delete.assert_called_once_with(
                "http://127.0.0.1:29100/mcp",
                headers={"mcp-session-id": "abc123"},
                timeout=1.0,
            )


class TestReaper(unittest.TestCase):
    def test_parse_record_and_reap_stale_and_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            servers = Path(tmp) / "servers"
            servers.mkdir()

            # Dead PID record should be unlinked without calling _stop
            dead = servers / "deadkey"
            dead.write_text("port=29100\npid=99999999\ncontext='antigravity'\nproject='/tmp/p'\n")
            dead_act = servers / "deadkey.activity"
            dead_act.touch()

            # Alive but idle record should call _stop and unlink
            idle = servers / "idlekey"
            idle.write_text("port=29101\npid=12345\ncontext='codex'\nproject='/tmp/p2'\n")
            idle_act = servers / "idlekey.activity"
            idle_act.touch()
            os.utime(idle_act, (1000.0, 1000.0))

            with (
                mock.patch.object(reaper, "SERVERS_DIR", servers),
                mock.patch.object(reaper, "IDLE_SECONDS", 1800),
                mock.patch.object(reaper, "_pid_alive", side_effect=lambda p: p == 12345),
                mock.patch.object(reaper, "_stop") as mock_stop,
            ):
                stopped = reaper.reap_once(now=5000.0)

            self.assertEqual(stopped, 1)
            mock_stop.assert_called_once_with(12345)
            self.assertFalse(dead.exists())
            self.assertFalse(dead_act.exists())
            self.assertFalse(idle.exists())
            self.assertFalse(idle_act.exists())


if __name__ == "__main__":
    unittest.main()
