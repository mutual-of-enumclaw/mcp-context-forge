# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/__init__.py
Copyright 2026
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

ContextForge - A flexible feature-rich FastAPI-based gateway for the Model Context Protocol (MCP).
"""

# ============================================================================
# mcp v1→v2 backwards-compat shim — TEMPORARY, until cpex >= <v2-compatible>
# ============================================================================
# The bundled `cpex.framework` plugin runtime still imports v1 names from the
# `mcp` SDK that were removed/renamed in 2.x (`McpError`, `streamable_http_client`,
# `mcp.server.fastmcp.FastMCP`). To let mcpgateway import against mcp 2.0.0a2
# before cpex is updated, we re-expose the v1 names at their old locations
# here, BEFORE any cpex import can fire transitively from mcpgateway.main.
#
# Each shim is no-op if the v1 name is already present, so the block is safe
# under both v1 and v2 SDKs and degrades cleanly once cpex catches up.
# Remove this block (and the import-time module-mutation it performs) once
# cpex publishes a release pinned to `mcp>=2,<3`.
# ============================================================================
import sys as _sys

try:
    import mcp as _mcp

    # (1) `McpError` → `MCPError` (exception class rename)
    if not hasattr(_mcp, "McpError") and hasattr(_mcp, "MCPError"):
        _mcp.McpError = _mcp.MCPError

    # (2) `streamable_http_client` → reroute to our compat wrapper. The wrapper
    #     preserves the v1 keyword surface (headers/timeout/auth/httpx_client_factory)
    #     and delegates to v2's `streamable_http_client(url, http_client=)`.
    import mcp.client.streamable_http as _sht_mod

    if not hasattr(_sht_mod, "streamable_http_client"):
        from mcpgateway.utils.streamable_http_compat import streamable_http_client as _v1_streamable_http_client

        _sht_mod.streamable_http_client = _v1_streamable_http_client

    # (3) `mcp.server.fastmcp` → `mcp.server.mcpserver` (module + class rename)
    try:
        import mcp.server.mcpserver as _mcpserver_mod

        _sys.modules.setdefault("mcp.server.fastmcp", _mcpserver_mod)
        if not hasattr(_mcpserver_mod, "FastMCP") and hasattr(_mcpserver_mod, "MCPServer"):
            _mcpserver_mod.FastMCP = _mcpserver_mod.MCPServer  # type: ignore[attr-defined]
    except ImportError:
        pass
except ImportError:
    # mcp SDK not installed — let the natural import error surface downstream.
    pass

del _sys
# ============================================================================
# end of v1→v2 compat shim
# ============================================================================

__author__ = "Mihai Criveti"
__copyright__ = "Copyright 2025"
__license__ = "Apache 2.0"
__version__ = "1.0.4"
__description__ = "IBM Consulting Assistants - Extensions API Library"
__url__ = "https://ibm.github.io/mcp-context-forge/"
__download_url__ = "https://github.com/IBM/mcp-context-forge"
__packages__ = ["mcpgateway"]
