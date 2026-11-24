"""Stdio MCP proxy onto one long-lived Serena streamable-HTTP server.

Stdout is the MCP protocol. Diagnostics go to stderr.
"""

from __future__ import annotations

import os
import sys
import time

import anyio
import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.message import SessionMessage
from mcp.types import (
    METHOD_NOT_FOUND,
    ErrorData,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

_TOUCH_INTERVAL = 5.0


def _touch_activity(path: str, last: list[float]) -> None:
    now = time.monotonic()
    if now - last[0] < _TOUCH_INTERVAL:
        return
    last[0] = now
    flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC
    fd = os.open(path, flags, 0o644)
    try:
        os.utime(path, None)
    finally:
        os.close(fd)


def _terminate_session(url: str, session_id: str | None, init_state: dict) -> None:
    if not session_id or init_state.get("terminated"):
        return
    init_state["terminated"] = True
    try:
        httpx.delete(url, headers={"mcp-session-id": session_id}, timeout=1.0)
    except Exception:
        pass


async def _pump_client_to_server(
    client_read,
    server_write,
    client_write,
    url: str,
    get_session_id,
    init_state: dict,
    on_item=None,
) -> None:
    try:
        async for item in client_read:
            if isinstance(item, Exception):
                print(f"serena-shared: transport error: {item}", file=sys.stderr)
                return
            if on_item is not None:
                on_item()

            root = item.message.root
            has_session = init_state["initialized"] or get_session_id() is not None

            if not has_session:
                if isinstance(root, JSONRPCRequest):
                    if root.method == "initialize":
                        init_state["init_id"] = root.id
                        await server_write.send(item)
                    else:
                        # Streamable-HTTP rejects non-initialize requests before a
                        # session exists with HTTP 400. Reply with a standard
                        # JSON-RPC error locally so clients (e.g. probing
                        # server/discover) can proceed to initialize.
                        err = JSONRPCError(
                            jsonrpc="2.0",
                            id=root.id,
                            error=ErrorData(
                                code=METHOD_NOT_FOUND,
                                message=f"Method not found: {root.method}",
                            ),
                        )
                        await client_write.send(SessionMessage(JSONRPCMessage(err)))
                    continue
                if isinstance(root, JSONRPCNotification):
                    # Drop pre-session notifications (e.g. notifications/roots/list_changed
                    # sent before initialize completes) so they don't trigger HTTP 400.
                    continue

            await server_write.send(item)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream):
        return
    finally:
        # Cleanly terminate our HTTP session on the shared server so it doesn't
        # linger in StreamableHTTPSessionManager, then exit immediately because
        # the idle HTTP GET stream does not unwind when stdin closes.
        _terminate_session(url, get_session_id(), init_state)
        os._exit(0)


async def _pump_server_to_client(
    server_read, client_write, url: str, get_session_id, init_state: dict
) -> None:
    try:
        async for item in server_read:
            if isinstance(item, Exception):
                print(f"serena-shared: transport error: {item}", file=sys.stderr)
                return
            root = item.message.root
            if (
                not init_state["initialized"]
                and init_state["init_id"] is not None
                and isinstance(root, JSONRPCResponse)
                and root.id == init_state["init_id"]
            ):
                init_state["initialized"] = True
            await client_write.send(item)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream):
        return
    finally:
        _terminate_session(url, get_session_id(), init_state)
        os._exit(0)


async def _run(url: str, activity_path: str | None) -> None:
    last = [0.0]

    def mark() -> None:
        if activity_path:
            _touch_activity(activity_path, last)

    # Tool calls wait on the POST response. Serena's tool timeout is 240s, and
    # the idle GET stream must stay open for a long agent session.
    timeout = httpx.Timeout(30.0, read=86400.0)
    client = create_mcp_http_client(timeout=timeout)
    init_state = {"initialized": False, "init_id": None, "terminated": False}
    async with client:
        async with stdio_server() as (client_read, client_write):
            async with streamable_http_client(
                url,
                http_client=client,
                terminate_on_close=False,
            ) as (server_read, server_write, get_session_id):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        _pump_client_to_server,
                        client_read,
                        server_write,
                        client_write,
                        url,
                        get_session_id,
                        init_state,
                        mark,
                    )
                    tg.start_soon(
                        _pump_server_to_client,
                        server_read,
                        client_write,
                        url,
                        get_session_id,
                        init_state,
                    )


def main() -> None:
    if len(sys.argv) not in (2, 3):
        print("usage: serena-shared-proxy URL [ACTIVITY_PATH]", file=sys.stderr)
        sys.exit(2)
    activity = sys.argv[2] if len(sys.argv) == 3 else None
    try:
        anyio.run(_run, sys.argv[1], activity)
    except* KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
