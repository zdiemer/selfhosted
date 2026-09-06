"""The console proxy: /vm/<session>/... -> http://<pod-ip>:8006/...

WHY A PATH PREFIX WORKS AT ALL, since it is the load-bearing assumption of the
whole no-per-VM-Ingress design and it is not obvious:

  - every asset in the noVNC page is referenced RELATIVELY (`app/ui.js`, not
    `/app/ui.js`), so they resolve under the prefix on their own;
  - app/ui.js sets `path = 'websockify'` and resolves it with
    `new URL(path, location.href)`, so a page served at /vm/<sid>/ asks for
    /vm/<sid>/websockify.

Verified end to end against a live pod before any of this was written: page 200,
app/ui.js 200, and the upgrade returning 101 Switching Protocols followed by the
RFB handshake bytes.

The trailing slash is therefore load-bearing. /vm/<sid> without it would make
the browser resolve `app/ui.js` against /vm/, so the redirect below is not
cosmetic.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import websockets
from fastapi import WebSocket
from starlette.responses import Response, StreamingResponse
from websockets.exceptions import ConnectionClosed

from vmlab import k8s

logger = logging.getLogger(__name__)

CONSOLE_PORT = 8006

# Hop-by-hop headers must not be forwarded. Content-Length and Content-Encoding
# are dropped too: httpx has already decoded the body, so passing the original
# values on would describe bytes we are no longer sending.
_DROP_REQUEST = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade"}
_DROP_RESPONSE = _DROP_REQUEST | {"content-length", "content-encoding"}

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # No connection limit worth tuning here; the cap on concurrent consoles
        # is the VM quota, which is far below anything httpx would care about.
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=False)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _upstream(session_id: str) -> str | None:
    """Resolve a session to a pod IP, refusing anything that is not a session.

    get_session() checks the component label, so a caller cannot aim the proxy
    at an arbitrary pod in the namespace — including this one — by guessing a
    name.
    """
    session = await asyncio.to_thread(k8s.get_session, session_id)
    if session is None or not session.get("podIP"):
        return None
    if session.get("phase") not in {"Running", "Pending"}:
        return None
    return session["podIP"]


async def forward_http(session_id: str, path: str, request) -> Response:
    ip = await _upstream(session_id)
    if ip is None:
        return Response("no such session", status_code=404)

    url = f"http://{ip}:{CONSOLE_PORT}/{path.lstrip('/')}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}

    try:
        upstream = await client().request(
            request.method,
            url,
            headers=headers,
            params=request.query_params,
            content=await request.body(),
        )
    except httpx.RequestError as exc:
        logger.info("console upstream error for %s: %s", session_id, exc)
        return Response("console is not up yet", status_code=502)

    out = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=out)


async def forward_ws(session_id: str, path: str, ws: WebSocket) -> None:
    ip = await _upstream(session_id)
    if ip is None:
        await ws.close(code=1008)
        return

    # noVNC offers `binary`; guacd-style subprotocol negotiation has to be
    # echoed back or the browser aborts the connection as unnegotiated.
    offered = ws.headers.get("sec-websocket-protocol", "")
    subprotocols = [p.strip() for p in offered.split(",") if p.strip()]

    url = f"ws://{ip}:{CONSOLE_PORT}/{path.lstrip('/')}"
    try:
        upstream = await websockets.connect(
            url,
            subprotocols=subprotocols or None,
            # The console is idle whenever nobody is typing, which is most of
            # the time. Default pings would tear those sessions down.
            ping_interval=20,
            ping_timeout=60,
            max_size=None,
            open_timeout=10,
        )
    except Exception as exc:
        logger.info("console websocket to %s failed: %s", session_id, exc)
        await ws.close(code=1011)
        return

    await ws.accept(subprotocol=upstream.subprotocol)
    # Somebody is watching, so the idle reaper should leave this alone.
    await asyncio.to_thread(k8s.touch, session_id)

    async def pump_down() -> None:
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                else:
                    await ws.send_text(message)
        except (ConnectionClosed, RuntimeError):
            pass

    async def pump_up() -> None:
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if (data := message.get("bytes")) is not None:
                    await upstream.send(data)
                elif (text := message.get("text")) is not None:
                    await upstream.send(text)
        except (ConnectionClosed, RuntimeError):
            pass

    async def keepalive() -> None:
        """Re-stamp last-seen while the console stays open, so a long session
        with nobody typing is not mistaken for an abandoned one."""
        try:
            while True:
                await asyncio.sleep(120)
                await asyncio.to_thread(k8s.touch, session_id)
        except asyncio.CancelledError:
            raise

    tasks = [asyncio.create_task(t()) for t in (pump_down, pump_up, keepalive)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
        try:
            await ws.close()
        except RuntimeError:
            pass
