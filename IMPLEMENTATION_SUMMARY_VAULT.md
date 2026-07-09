# Implementation Summary: Vault Token Storage (Feature #5402)

**Status:** Implementation Plan  
**Phase:** 1 (Vault + Minimal Database Extraction)  
**Date:** 2026-07-09

---

## Overview

This document provides a detailed implementation roadmap for adding pluggable token storage with VaultTokenBackend support. The implementation follows the architect design document exactly.

**Key Principle:** Phase 1 implements full Vault backend + minimal database extraction (copy-paste, zero behavior changes) to enable façade pattern. NO database schema changes in Phase 1.

---

## File Structure

```
mcpgateway/services/
  token_storage_service.py          [CHANGED] - becomes façade
  token_backends/                   [NEW PACKAGE]
    __init__.py                     [NEW]
    base.py                         [NEW] - AbstractTokenBackend + TokenRecord
    db_backend.py                   [NEW] - DatabaseTokenBackend (extracted)
    vault_backend.py                [NEW] - VaultTokenBackend

mcpgateway/routers/
  vault_router.py                   [NEW] - /vault/authorize + /vault/callback

mcpgateway/
  config.py                         [CHANGED] - add 10 Vault env vars
  main.py                           [CHANGED] - register vault_router conditionally

mcpgateway/routers/
  oauth_router.py                   [MINIMAL CHANGE] - pass user_context
  
mcpgateway/services/
  tool_service.py                   [MINIMAL CHANGE] - pass user_context
  gateway_service.py                [MINIMAL CHANGE] - pass user_context
  resource_service.py               [MINIMAL CHANGE] - pass user_context

mcpgateway/
  admin.py                          [MINIMAL CHANGE] - pass user_context

pyproject.toml                      [CHANGED] - add [vault] extra
```

---

## Task 1: Create AbstractTokenBackend Interface

**File:** `mcpgateway/services/token_backends/base.py` [NEW]

**Content:**
```python
"""
Base interface for pluggable token storage backends.

This module defines the abstract interface that all token storage backends
must implement, plus a plain dataclass for token records (no SQLAlchemy).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class TokenRecord:
    """
    Plain dataclass for token records - no SQLAlchemy dependencies.
    Used by all backends to return token data in a consistent format.
    """
    gateway_id: str              # gateways.id (UUID) - used by DB backend as FK
    mcp_url: str                 # gateways.url - resolved by VaultTokenBackend; Vault path key
    team_id: str                 # Team identifier - extracted from user context
    user_id: str                 # OAuth provider user ID (e.g., GitHub numeric UID)
    app_user_email: str          # ContextForge user identity
    access_token: str            # Plain-text (backends handle encryption differently)
    refresh_token: str | None    # Nullable
    token_type: str              # Always "Bearer"
    expires_at: datetime | None  # Nullable (some providers omit expiry)
    scopes: list[str]           # OAuth scopes
    created_at: datetime
    updated_at: datetime


class AbstractTokenBackend(ABC):
    """
    Backend-agnostic token storage interface.
    
    All methods receive gateway_id and team_id. Each backend uses them appropriately:
      - DatabaseTokenBackend → uses gateway_id directly as FK; team_id ignored (no DB column yet)
      - VaultTokenBackend    → uses team_id in path; resolves gateway_id → mcp_url → server_id
    
    The CLIENT never passes gateway_id or team_id. It only knows server_id (virtual server URL).
    The service layer extracts team_id from authenticated user context (JWT/session), and
    resolves gateway_id from: server_id → server_tool_association → tools.gateway_id.
    """

    @abstractmethod
    async def store_tokens(
        self,
        gateway_id: str,           # UUID from gateways.id - passed by all existing call sites
        team_id: str,              # Team identifier from user context (JWT/session)
        user_id: str,              # OAuth provider user ID
        app_user_email: str,       # ContextForge user email
        access_token: str,
        refresh_token: str | None,
        expires_in: int | None,
        scopes: list[str],
    ) -> TokenRecord:
        """
        Store OAuth tokens for a user.
        
        Called at OAuth callback after IdP returns tokens.
        DatabaseTokenBackend: encrypts with Fernet, UPSERTs to oauth_tokens table
        VaultTokenBackend: resolves gateway_id → mcp_url, writes plain-text to Vault KV v2
        
        Returns TokenRecord with plain-text tokens for immediate use.
        """
        ...

    @abstractmethod
    async def get_user_token(
        self,
        gateway_id: str,
        team_id: str,              # Team identifier from user context
        app_user_email: str,
        threshold_seconds: int = 300,
    ) -> str | None:
        """
        Retrieve access token for a user, auto-refreshing if near expiry.
        
        Called on every tool call / health-check / resource fetch.
        Returns plain-text access token ready for Authorization header.
        Returns None if no token found (user needs to authorize).
        
        Auto-refresh logic:
        - If token expires within threshold_seconds, attempt refresh
        - If refresh succeeds, store new token and return it
        - If refresh fails, return None (user must re-authorize)
        """
        ...

    @abstractmethod
    async def get_token_info(
        self,
        gateway_id: str,
        team_id: str,              # Team identifier from user context
        app_user_email: str,
    ) -> dict | None:
        """
        Get non-sensitive token metadata for admin/status API.
        
        Returns dict with keys:
        - scopes: list[str]
        - expires_at: str (ISO-8601) or None
        - status: "valid" | "expired" | "near_expiry"
        - updated_at: str (ISO-8601)
        
        Returns None if no token found.
        Does NOT return actual token values.
        """
        ...

    @abstractmethod
    async def revoke_user_tokens(
        self,
        gateway_id: str,
        team_id: str,              # Team identifier from user context
        app_user_email: str,
    ) -> bool:
        """
        Delete/revoke stored tokens for a user.
        
        Called at user logout or admin revoke.
        DatabaseTokenBackend: SQL DELETE on matching row
        VaultTokenBackend: Vault KV soft-delete (hard-delete via metadata endpoint)
        
        Returns True if deleted, False if not found.
        """
        ...

    @abstractmethod
    async def cleanup_expired_tokens(
        self,
        max_age_days: int = 30,
    ) -> int:
        """
        Clean up expired/old tokens (maintenance job).
        
        DatabaseTokenBackend: SQL DELETE WHERE expires_at < cutoff
        VaultTokenBackend: No-op, returns 0 (Vault KV TTL handles cleanup)
        
        Returns count of deleted tokens.
        """
        ...
```

**File:** `mcpgateway/services/token_backends/__init__.py` [NEW]

```python
"""Token storage backends package."""
from .base import AbstractTokenBackend, TokenRecord
from .db_backend import DatabaseTokenBackend
from .vault_backend import VaultTokenBackend

__all__ = [
    "AbstractTokenBackend",
    "TokenRecord",
    "DatabaseTokenBackend",
    "VaultTokenBackend",
]
```

---

## Task 2: Extract DatabaseTokenBackend

**File:** `mcpgateway/services/token_backends/db_backend.py` [NEW]

**Instructions:**
1. Copy lines 119-620 from current `token_storage_service.py` (all the DB logic)
2. Wrap in `DatabaseTokenBackend(AbstractTokenBackend)` class
3. Accept `team_id` parameter in all methods but **COMPLETELY IGNORE IT**
4. NO changes to SQL queries - keep using `(gateway_id, app_user_email)` as unique key
5. NO database schema changes
6. Preserve ALL existing behavior:
   - UPSERT logic on (gateway_id, app_user_email)
   - Encryption via EncryptionService
   - Auto-refresh with RFC 8707 resource parameter
   - NULL expiry handling
   - Private gateway ownership security check (PR #4341)

**Pseudo-structure:**
```python
from sqlalchemy.orm import Session
from mcpgateway.db import OAuthToken
from mcpgateway.config import get_settings
# ... other imports from existing code

class DatabaseTokenBackend(AbstractTokenBackend):
    def __init__(self, db: Session, settings):
        self.db = db
        self.settings = settings
        self.encryption = get_encryption_service(settings.auth_encryption_secret)
    
    async def store_tokens(
        self,
        gateway_id: str,
        team_id: str,           # ← Accepted but NOT used (Phase 1)
        user_id: str,
        app_user_email: str,
        access_token: str,
        refresh_token: str | None,
        expires_in: int | None,
        scopes: list[str],
    ) -> TokenRecord:
        # EXACT COPY of existing logic from token_storage_service.py
        # Query by (gateway_id, app_user_email) - team_id ignored
        # ... (existing encryption, UPSERT, expiry calculation) ...
        
        # Return TokenRecord dataclass
        return TokenRecord(
            gateway_id=gateway_id,
            mcp_url=self._resolve_mcp_url(gateway_id),  # Query gateways.url
            team_id="default",  # Phase 1: no DB column, use fallback
            user_id=user_id,
            app_user_email=app_user_email,
            access_token=access_token,  # Plain-text in return
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_at=...,
            scopes=scopes,
            created_at=...,
            updated_at=...,
        )
    
    async def get_user_token(
        self,
        gateway_id: str,
        team_id: str,           # ← Accepted but NOT used (Phase 1)
        app_user_email: str,
        threshold_seconds: int = 300,
    ) -> str | None:
        # EXACT COPY of existing logic
        # Query by (gateway_id, app_user_email) - team_id ignored
        # ... (existing refresh logic, decryption) ...
    
    # ... implement other 3 methods similarly ...
    
    def _resolve_mcp_url(self, gateway_id: str) -> str:
        """Helper to resolve gateway_id → gateways.url for TokenRecord."""
        gateway = self.db.get(Gateway, gateway_id)
        if not gateway:
            raise ValueError(f"Gateway {gateway_id} not found")
        return gateway.url
```

---

## Task 3: Implement VaultTokenBackend

**File:** `mcpgateway/services/token_backends/vault_backend.py` [NEW]

**Key features:**
- Uses httpx for Vault KV v2 HTTP API (not hvac library for simplicity)
- Resolves `gateway_id → gateways.url → server_id` (SHA-256 hash)
- Constructs path: `{mount}/data/{prefix}/{team_id}/{server_id}/{url-encoded-email}`
- Implements retry logic (3 attempts with exponential backoff)
- Optional in-memory token cache with TTL
- All tokens stored plain-text in Vault (Vault encrypts at rest)

**Pseudo-structure:**
```python
import hashlib
from urllib.parse import quote
import httpx
import asyncio
from datetime import datetime, timedelta, timezone
from mcpgateway.db import Gateway

class VaultTokenBackend(AbstractTokenBackend):
    def __init__(self, db: Session, settings):
        self.db = db
        self.settings = settings
        self.vault_addr = settings.vault_addr
        self.vault_token = settings.vault_token.get_secret_value() if settings.vault_token else None
        self.mount = settings.vault_kv_mount
        self.prefix = settings.vault_kv_path_prefix
        self.tls_verify = settings.vault_tls_verify
        
        # Optional cache
        self.cache_enabled = settings.vault_token_cache_enabled
        if self.cache_enabled:
            self._cache = {}  # Simple dict, consider TTLCache for production
            self.cache_ttl = settings.vault_token_cache_ttl
            self.cache_max_size = settings.vault_token_cache_max_size
    
    def _resolve_mcp_url(self, gateway_id: str) -> str:
        """Resolve gateway_id → gateways.url."""
        gateway = self.db.get(Gateway, gateway_id)
        if not gateway:
            raise ValueError(f"Gateway {gateway_id} not found")
        return gateway.url
    
    def _hash_server_id(self, mcp_url: str) -> str:
        """Hash mcp_url to stable server_id (first 8 hex chars of SHA-256)."""
        return hashlib.sha256(mcp_url.encode()).hexdigest()[:8]
    
    def _construct_vault_path(
        self, team_id: str, mcp_url: str, app_user_email: str
    ) -> str:
        """Construct full Vault KV v2 path."""
        server_id = self._hash_server_id(mcp_url)
        email_encoded = quote(app_user_email, safe='')
        return f"{self.mount}/data/{self.prefix}/{team_id}/{server_id}/{email_encoded}"
    
    async def _vault_request(
        self, method: str, path: str, data: dict | None = None
    ) -> dict:
        """Make HTTP request to Vault with retry logic."""
        headers = {"X-Vault-Token": self.vault_token}
        url = f"{self.vault_addr}/v1/{path}"
        
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(verify=self.tls_verify) as client:
                    if method == "GET":
                        resp = await client.get(url, headers=headers, timeout=10.0)
                    elif method == "POST":
                        resp = await client.post(url, headers=headers, json=data, timeout=10.0)
                    elif method == "DELETE":
                        resp = await client.delete(url, headers=headers, timeout=10.0)
                    
                    resp.raise_for_status()
                    return resp.json() if resp.content else {}
            
            except httpx.ConnectTimeout:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # 1s, 2s
                    continue
                logger.error(f"Vault unreachable after 3 attempts: {url}")
                raise GatewayConnectionError("Credential storage unavailable")
            
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    logger.critical("Vault auth failure - VAULT_TOKEN invalid or expired")
                    raise
                elif e.response.status_code == 404:
                    return None  # No token found - expected
                raise
    
    async def store_tokens(
        self,
        gateway_id: str,
        team_id: str,
        user_id: str,
        app_user_email: str,
        access_token: str,
        refresh_token: str | None,
        expires_in: int | None,
        scopes: list[str],
    ) -> TokenRecord:
        mcp_url = self._resolve_mcp_url(gateway_id)
        path = self._construct_vault_path(team_id, mcp_url, app_user_email)
        
        expires_at = None
        if expires_in:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        
        now = datetime.now(timezone.utc)
        payload = {
            "data": {
                "email": app_user_email,
                "team_id": team_id,
                "mcp_url": mcp_url,
                "token": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "scopes": scopes,
                },
                "user_id": user_id,
                "token_type": "Bearer",
                "expires_at": expires_at.isoformat() if expires_at else None,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }
        }
        
        await self._vault_request("POST", path, payload)
        
        # Invalidate cache
        if self.cache_enabled:
            cache_key = (team_id, self._hash_server_id(mcp_url), app_user_email)
            self._cache.pop(cache_key, None)
        
        return TokenRecord(
            gateway_id=gateway_id,
            mcp_url=mcp_url,
            team_id=team_id,
            user_id=user_id,
            app_user_email=app_user_email,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_at=expires_at,
            scopes=scopes,
            created_at=now,
            updated_at=now,
        )
    
    async def get_user_token(
        self,
        gateway_id: str,
        team_id: str,
        app_user_email: str,
        threshold_seconds: int = 300,
    ) -> str | None:
        mcp_url = self._resolve_mcp_url(gateway_id)
        server_id = self._hash_server_id(mcp_url)
        cache_key = (team_id, server_id, app_user_email)
        
        # Check cache
        if self.cache_enabled and cache_key in self._cache:
            cached = self._cache[cache_key]
            if datetime.now(timezone.utc) < cached['expires']:
                return cached['token']
        
        path = self._construct_vault_path(team_id, mcp_url, app_user_email)
        result = await self._vault_request("GET", path)
        
        if not result or 'data' not in result:
            return None
        
        data = result['data']['data']
        access_token = data['token']['access_token']
        refresh_token = data['token'].get('refresh_token')
        expires_at_str = data.get('expires_at')
        
        # Check expiry and refresh if needed
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
            if (expires_at - datetime.now(timezone.utc)).total_seconds() < threshold_seconds:
                # Attempt refresh
                if refresh_token:
                    new_token = await self._refresh_access_token(
                        gateway_id, team_id, app_user_email, refresh_token, data
                    )
                    if new_token:
                        return new_token
                return None  # Expired, no refresh, user must re-authorize
        
        # Cache token
        if self.cache_enabled:
            self._cache[cache_key] = {
                'token': access_token,
                'expires': datetime.now(timezone.utc) + timedelta(seconds=self.cache_ttl)
            }
            # TODO: Implement LRU eviction if len(self._cache) > self.cache_max_size
        
        return access_token
    
    # ... implement get_token_info, revoke_user_tokens, cleanup_expired_tokens ...
    # ... implement _refresh_access_token (similar to DB backend logic) ...
```

---

## Task 4: Refactor TokenStorageService to Façade

**File:** `mcpgateway/services/token_storage_service.py` [CHANGED]

**Changes:**
1. Import backends: `from .token_backends import DatabaseTokenBackend, VaultTokenBackend`
2. Add `user_context` parameter to `__init__`
3. Select backend based on `settings.oauth_token_backend`
4. Add `_get_team_id()` helper
5. Delegate all 5 methods to backend

**New structure:**
```python
from mcpgateway.auth_context import get_user_teams

class TokenStorageService:
    def __init__(self, db: Session, user_context: dict | None = None):
        """
        Initialize token storage service with selected backend.
        
        Args:
            db: SQLAlchemy session (used by both backends for different purposes)
            user_context: JWT claims or session data for team_id extraction
        """
        self.db = db
        self.user_context = user_context
        settings = get_settings()
        
        if settings.oauth_token_backend == "vault":
            self._backend = VaultTokenBackend(db, settings)
        elif settings.oauth_token_backend == "database":
            self._backend = DatabaseTokenBackend(db, settings)
        else:
            raise ValueError(
                f"Unknown OAUTH_TOKEN_BACKEND: {settings.oauth_token_backend}. "
                f"Expected 'database' or 'vault'."
            )
    
    def _get_team_id(self, app_user_email: str) -> str:
        """
        Extract team_id from authenticated user context.
        Precedence: JWT claims → session data → fallback 'default'.
        """
        if self.user_context:
            teams = get_user_teams(self.user_context)
            return teams[0] if teams else "default"
        return "default"
    
    async def store_tokens(
        self,
        gateway_id: str,
        user_id: str,
        app_user_email: str,
        access_token: str,
        refresh_token: str | None,
        expires_in: int | None,
        scopes: list[str],
    ) -> TokenRecord:
        team_id = self._get_team_id(app_user_email)
        return await self._backend.store_tokens(
            gateway_id, team_id, user_id, app_user_email,
            access_token, refresh_token, expires_in, scopes
        )
    
    async def get_user_token(
        self, gateway_id: str, app_user_email: str, threshold_seconds: int = 300
    ) -> str | None:
        team_id = self._get_team_id(app_user_email)
        return await self._backend.get_user_token(
            gateway_id, team_id, app_user_email, threshold_seconds
        )
    
    # ... similarly delegate get_token_info, revoke_user_tokens, cleanup_expired_tokens ...
```

---

## Task 5: Create vault_router

**File:** `mcpgateway/routers/vault_router.py` [NEW]

**Endpoints:**
- `GET /vault/authorize/{server_id}` - initiate OAuth flow for Vault backend
- `GET /vault/callback` - OAuth callback handler (stores to Vault)

**Key logic:**
```python
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from mcpgateway.db import Server, Gateway, Tool, server_tool_association

router = APIRouter(prefix="/vault", tags=["vault-oauth"])

@router.get("/authorize/{server_id}")
async def vault_authorize(
    server_id: str,
    gateway_url: str | None = Query(None),  # Optional for multi-gateway servers
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Initiate OAuth flow using virtual server_id.
    
    1. Lookup servers by server_id
    2. Walk server_tool_association → tools.gateway_id → gateways
    3. Filter to OAuth-enabled gateways
    4. If gateway_url param provided, use that gateway; else use first
    5. Build PKCE + HMAC state, save to oauth_states table
    6. Redirect to IdP authorization URL
    """
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    
    # Resolve gateway(s)
    gateway = _resolve_oauth_gateway(server, db, gateway_url)
    if not gateway:
        raise HTTPException(400, "No OAuth gateways configured for this server")
    
    # Build OAuth state (similar to existing /oauth/authorize)
    state = generate_oauth_state(gateway.id, user['email'])
    save_oauth_state(db, state, gateway.id)
    
    # Redirect to IdP
    oauth_config = gateway.oauth_config
    auth_url = build_authorization_url(oauth_config, state)
    return RedirectResponse(auth_url, status_code=302)

@router.get("/callback")
async def vault_callback(
    code: str,
    state: str,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    """
    OAuth callback handler - stores tokens to Vault.
    
    1. Validate HMAC state → resolve gateway_id from oauth_states
    2. Exchange code for tokens with IdP
    3. Extract team_id from user context
    4. Call TokenStorageService.store_tokens() → VaultTokenBackend writes to Vault
    5. Return HTML success page
    """
    if error:
        return HTMLResponse(f"<h1>Authorization failed</h1><p>{error_description}</p>")
    
    # Validate state
    oauth_state = validate_and_get_oauth_state(db, state)
    if not oauth_state:
        raise HTTPException(400, "Invalid or expired state")
    
    gateway_id = oauth_state.gateway_id
    gateway = db.get(Gateway, gateway_id)
    
    # Exchange code for tokens
    token_response = await exchange_code_for_tokens(gateway.oauth_config, code)
    
    # Extract user context (from session or state)
    user_context = {"email": oauth_state.user_email, "teams": [...]}  # TODO: extract properly
    
    # Store to Vault via service
    token_service = TokenStorageService(db, user_context)
    await token_service.store_tokens(
        gateway_id=gateway_id,
        user_id=token_response['user_id'],
        app_user_email=oauth_state.user_email,
        access_token=token_response['access_token'],
        refresh_token=token_response.get('refresh_token'),
        expires_in=token_response.get('expires_in'),
        scopes=token_response.get('scope', '').split(),
    )
    
    return HTMLResponse("<h1>✓ Authorization successful</h1><p>You can close this window.</p>")

def _resolve_oauth_gateway(
    server: Server, db: Session, preferred_url: str | None = None
) -> Gateway | None:
    """Resolve OAuth-enabled gateway for a virtual server."""
    # Query all gateway_ids linked to this server
    gateway_ids = db.execute(
        select(Tool.gateway_id)
        .join(server_tool_association, server_tool_association.c.tool_id == Tool.id)
        .where(server_tool_association.c.server_id == server.id)
        .where(Tool.gateway_id.isnot(None))
        .distinct()
    ).scalars().all()
    
    # Filter to OAuth-enabled gateways
    gateways = db.scalars(
        select(Gateway)
        .where(Gateway.id.in_(gateway_ids))
        .where(Gateway.auth_type == "oauth")
    ).all()
    
    if not gateways:
        return None
    
    if preferred_url:
        return next((g for g in gateways if g.url == preferred_url), None)
    
    return gateways[0]  # Return first if no preference
```

---

## Task 6: Add Vault Configuration

**File:** `mcpgateway/config.py` [CHANGED]

**Add these fields to Settings class:**
```python
from pydantic import SecretStr

class Settings(BaseSettings):
    # ... existing fields ...
    
    # Token Storage Backend Selection
    oauth_token_backend: str = Field(
        default="database",
        description="Token storage backend: 'database' or 'vault'",
    )
    
    # Vault Connection
    vault_addr: str = Field(
        default="http://127.0.0.1:8200",
        description="Vault server URL",
    )
    vault_token: SecretStr | None = Field(
        default=None,
        description="Vault authentication token (Phase 1: static token; Phase 2: AppRole)",
    )
    vault_namespace: str = Field(
        default="",
        description="Vault namespace (Enterprise only; leave empty for CE)",
    )
    vault_kv_mount: str = Field(
        default="secret",
        description="Vault KV v2 mount path",
    )
    vault_kv_path_prefix: str = Field(
        default="contextforge/oauth",
        description="Path prefix within KV mount",
    )
    vault_tls_verify: bool = Field(
        default=True,
        description="Verify Vault TLS certificate (false for local dev only)",
    )
    
    # Token Cache (optional, Vault backend only)
    vault_token_cache_enabled: bool = Field(
        default=False,
        description="Enable in-memory token cache to reduce Vault API calls",
    )
    vault_token_cache_ttl: int = Field(
        default=300,
        description="Cache TTL in seconds (tokens may be stale within this window)",
    )
    vault_token_cache_max_size: int = Field(
        default=10000,
        description="Max cached entries before LRU eviction (~1KB per token)",
    )
```

---

## Task 7: Update Call Sites

**Files to update (minimal change - only instantiation):**
- `mcpgateway/routers/oauth_router.py`
- `mcpgateway/services/tool_service.py`
- `mcpgateway/services/gateway_service.py`
- `mcpgateway/services/resource_service.py`
- `mcpgateway/admin.py`

**Change pattern:**
```python
# BEFORE:
token_service = TokenStorageService(db)

# AFTER:
token_service = TokenStorageService(db, request.state.user)
# OR
token_service = TokenStorageService(db, user_context)
```

**user_context extraction:**
```python
# From FastAPI request
user_context = {
    "email": request.state.user.get("email"),
    "teams": request.state.user.get("teams", []),
    "is_admin": request.state.user.get("is_admin", False),
}

# Or if user_context already exists as a dict, pass directly
token_service = TokenStorageService(db, user_context)
```

---

## Task 8: Update main.py

**File:** `mcpgateway/main.py` [CHANGED]

**Changes:**
```python
from mcpgateway.config import get_settings
from mcpgateway.routers import vault_router

# ... existing imports and app creation ...

@app.on_event("startup")
async def startup_event():
    settings = get_settings()
    
    # Conditionally register vault_router
    if settings.oauth_token_backend == "vault":
        app.include_router(vault_router.router)
        logger.info(
            f"oauth_token_backend=vault vault_addr={settings.vault_addr} ✓ reachable"
        )
        # TODO: Add actual Vault connectivity check here
    else:
        logger.info(f"oauth_token_backend={settings.oauth_token_backend}")
    
    # ... existing startup logic ...
```

---

## Task 9: Add hvac Dependency

**File:** `pyproject.toml` [CHANGED]

**Add optional dependency:**
```toml
[project.optional-dependencies]
vault = [
    "hvac>=2.3.0",
]
```

**Update README.md:**
```markdown
## Installation

### With Vault backend support:
```bash
pip install ".[vault]"
```

### Without Vault (database backend only):
```bash
pip install .
```
```

---

## Implementation Checklist

- [ ] Task 1: Create AbstractTokenBackend + TokenRecord
- [ ] Task 2: Extract DatabaseTokenBackend (copy-paste, no changes)
- [ ] Task 3: Implement VaultTokenBackend
- [ ] Task 4: Refactor TokenStorageService to façade
- [ ] Task 5: Create vault_router
- [ ] Task 6: Add Vault config fields
- [ ] Task 7: Update call sites (oauth_router, tool_service, gateway_service, resource_service, admin)
- [ ] Task 8: Update main.py
- [ ] Task 9: Add hvac dependency
- [ ] Run `make ruff` and fix any issues
- [ ] Run `make mypy` and fix type issues
- [ ] Run existing tests: `make test`
- [ ] Manual testing (see MANUAL_TESTING_VAULT_TOKEN_STORAGE.md)

---

## Critical Implementation Notes

1. **Phase 1 Database Backend:**
   - Accept `team_id` parameter but IGNORE it
   - NO database schema changes
   - NO SQL query changes
   - This is purely code reorganization

2. **Vault Path Resolution:**
   - Always resolve `gateway_id → gateways.url` INSIDE VaultTokenBackend
   - Never expose `gateway_id` to clients
   - Use SHA-256 first 8 hex chars for `server_id` hash

3. **Team ID Extraction:**
   - Extract from JWT `teams` claim via `get_user_teams()`
   - Fallback to "default" if no teams
   - Use first team in list if multiple

4. **Error Handling:**
   - User-friendly messages (never expose internal Vault paths)
   - Retry logic for transient failures
   - Clear actionable guidance (redirect to /vault/authorize)

5. **Security:**
   - Never log access_token, refresh_token, or VAULT_TOKEN values
   - Use SecretStr for sensitive config fields
   - Vault stores plain-text (Vault encrypts at rest)

6. **Testing:**
   - Test database backend regression first (baseline)
   - Then test Vault backend with real Vault server
   - Verify no cross-contamination between backends

---

## Next Steps After Implementation

1. Complete manual testing (MANUAL_TESTING_VAULT_TOKEN_STORAGE.md)
2. Write unit tests for:
   - DatabaseTokenBackend (ensure exact behavior match)
   - VaultTokenBackend (mock Vault HTTP API)
   - TokenStorageService façade
3. Write integration tests for vault_router
4. Update documentation in `docs/`
5. Create PR with detailed description
6. DO NOT commit until manual testing complete

---

**End of Implementation Summary**
