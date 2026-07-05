# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/utils/mcp_proxy_client.py
Copyright 2026
SPDX-License-Identifier: Apache-2.0

MCP v2 proxy client for direct-proxy calls.

Replaces the old ``streamable_http_client`` + ``ClientSession`` pattern with
the MCP v2 ``Client`` class.

Usage::

    async with mcp_proxy_client(url, headers, timeout) as client:
        tools = await client.list_tools(meta=meta)
        resources = await client.list_resources()
        result = await client.read_resource(uri, meta=meta)
        result = await client.call_tool(name, arguments, meta=meta)
"""

from __future__ import annotations

import contextlib
import httpx
import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mcp import Client

logger = logging.getLogger(__name__)

__all__ = ["mcp_proxy_client"]

# Re-export for use by transport code that still references streamable_http_client
# from the SDK directly when needed.
from mcp.client.streamable_http import streamable_http_client  # noqa: E402  # SDK-only re-export for this module.


@contextlib.asynccontextmanager
async def mcp_proxy_client(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    httpx_client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> "Client":  # type: ignore[misc]
    """Yield an MCP v2 ``Client`` connected via streamable-http transport.

    The ``Client`` auto-initializes (via ``initialize()`` or auto-negotiation)
    before ``yield`` returns.  No manual ``session.initialize()`` call needed.

    The underlying ``httpx.AsyncClient`` stays alive for the transport's
    lifetime because the transport holds a reference to it.

    Args:
        url: Gateway or server URL to connect to.
        headers: HTTP headers to include in the request.
        timeout: Overall timeout for operations (passed through to factory).
        httpx_client_factory: Optional callable returning a configured
            ``httpx.AsyncClient``.  Receives keyword args
            ``(headers, timeout, auth)`` matching the compat wrapper.

    Yields:
        An ``mcp.Client`` instance with an established transport.

    Raises:
        RuntimeError: If the transport initialization fails.
    """
    if httpx_client_factory is not None:
        try:
            http_client = httpx_client_factory(headers=headers, timeout=timeout, auth=None)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to create httpx.AsyncClient: {exc}"
            ) from exc
    else:
        # Use sensible timeout defaults for MCP transport.
        # Connect: keep short to fail fast.  Read: must cover the full RPC.
        connect_timeout = min(timeout, 10.0)
        read_timeout = max(timeout, 30.0)
        http_client = httpx.AsyncClient(
            headers=headers or {},
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
            ),
        )

    async with http_client:
        # streamable_http_client is an async generator, context-manager usable.
        # The transport needs the http_client alive for its entire lifetime.
        scm = streamable_http_client(url, http_client=http_client)
        async with scm as streams:
            transport = streams
            client = Client(read_stream=transport[0], write_stream=transport[1])
            async with client:
                yield client
