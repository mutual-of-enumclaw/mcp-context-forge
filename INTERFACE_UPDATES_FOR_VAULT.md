# Interface Updates Required for Vault Integration

## Problem

The current `AbstractTokenBackend` interface design does not include `team_id` as a parameter, but the Vault secret schema **requires** `team_id` in the path:

```
<VAULT_KV_MOUNT>/data/<VAULT_KV_PATH_PREFIX>/<team_id>/<server_id>/<email>
```

Without `team_id` in the interface, `VaultTokenBackend` cannot construct the correct Vault path.

## Required Changes

### 1. Update `AbstractTokenBackend` Interface

**File: `mcpgateway/services/token_backends/base.py`**

#### 1.1 Add `team_id` to `TokenRecord` dataclass

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class TokenRecord:
    """Plain dataclass — no SQLAlchemy dependencies."""
    gateway_id: str           # gateways.id (UUID) — used by DB backend
    mcp_url: str              # gateways.url — resolved by VaultTokenBackend; used as Vault path key
    team_id: str              # NEW: team identifier from user context
    user_id: str              # OAuth provider user ID
    app_user_email: str       # ContextForge user identity
    access_token: str         # Plain-text (each backend handles encryption differently)
    refresh_token: str | None
    token_type: str           # Always "Bearer"
    expires_at: datetime | None
    scopes: list[str]
    created_at: datetime
    updated_at: datetime
```

#### 1.2 Add `team_id` parameter to all interface methods

```python
class AbstractTokenBackend(ABC):
    """
    Backend-agnostic token storage interface.

    All methods receive gateway_id and team_id. Each backend uses them appropriately:
      DatabaseTokenBackend  → uses gateway_id directly as FK; team_id can be optional/ignored (DB schema doesn't have it yet)
      VaultTokenBackend     → uses team_id in path; resolves gateway_id → mcp_url for server_id segment

    The CLIENT never passes gateway_id or team_id directly. The service layer extracts:
      - team_id from authenticated user context (JWT claims or session data)
      - gateway_id from server_id lookup chain: server_id → server_tool_association → tools.gateway_id
    """

    # Write — called at OAuth callback after IdP returns tokens
    @abstractmethod
    async def store_tokens(
        self,
        gateway_id: str,        # UUID from gateways.id — passed by all existing call sites
        team_id: str,           # NEW: team identifier from user context
        user_id: str,           # OAuth provider user ID
        app_user_email: str,    # ContextForge user email
        access_token: str,
        refresh_token: str | None,
        expires_in: int | None,
        scopes: list[str],
    ) -> TokenRecord:
        """Store OAuth tokens for a user.
        
        Args:
            gateway_id: Gateway UUID (from gateways.id)
            team_id: Team identifier from authenticated user context
            user_id: OAuth provider user ID
            app_user_email: ContextForge user email
            access_token: OAuth access token
            refresh_token: OAuth refresh token (optional)
            expires_in: Token expiry in seconds (optional)
            scopes: List of OAuth scopes
            
        Returns:
            TokenRecord with stored token data
        """
        ...

    # Read — called on every tool call / health-check / resource fetch
    @abstractmethod
    async def get_user_token(
        self,
        gateway_id: str,
        team_id: str,           # NEW: team identifier
        app_user_email: str,
        threshold_seconds: int = 300,
    ) -> str | None:
        """Retrieve access token for a user, auto-refreshing if near expiry.
        
        Args:
            gateway_id: Gateway UUID
            team_id: Team identifier from authenticated user context
            app_user_email: ContextForge user email
            threshold_seconds: Refresh token if expires within this many seconds
            
        Returns:
            Access token string, or None if not found
        """
        ...

    # Metadata — called by admin status API
    @abstractmethod
    async def get_token_info(
        self,
        gateway_id: str,
        team_id: str,           # NEW: team identifier
        app_user_email: str,
    ) -> dict | None:
        """Get non-sensitive token metadata.
        
        Args:
            gateway_id: Gateway UUID
            team_id: Team identifier
            app_user_email: ContextForge user email
            
        Returns:
            Dict with scopes, expiry, status (no tokens), or None if not found
        """
        ...

    # Revocation — called at user logout or admin revoke
    @abstractmethod
    async def revoke_user_tokens(
        self,
        gateway_id: str,
        team_id: str,           # NEW: team identifier
        app_user_email: str,
    ) -> bool:
        """Delete stored tokens for a user.
        
        Args:
            gateway_id: Gateway UUID
            team_id: Team identifier
            app_user_email: ContextForge user email
            
        Returns:
            True if tokens were deleted, False if not found
        """
        ...

    # Maintenance — called by scheduled cleanup job
    @abstractmethod
    async def cleanup_expired_tokens(
        self,
        max_age_days: int = 30,
    ) -> int:
        """Clean up expired tokens.
        
        DatabaseTokenBackend: deletes expired DB rows older than max_age_days
        VaultTokenBackend:    returns 0 — Vault KV TTL handles cleanup automatically
        
        Args:
            max_age_days: Maximum age in days for token retention
            
        Returns:
            Number of tokens cleaned up
        """
        ...
```

### 2. Update `TokenStorageService` Façade

**File: `mcpgateway/services/token_storage_service.py`**

The façade needs to:
1. Extract `team_id` from authenticated user context
2. Pass `team_id` to all backend method calls

```python
from typing import Optional
from sqlalchemy.orm import Session
from mcpgateway.config import get_settings
from mcpgateway.services.token_backends import (
    AbstractTokenBackend,
    DatabaseTokenBackend,
    VaultTokenBackend,
)
from mcpgateway.auth_context import get_user_teams  # Helper to extract team from user context


class TokenStorageService:
    """
    Façade for token storage operations.
    
    Responsibilities:
    1. Select appropriate backend based on OAUTH_TOKEN_BACKEND config
    2. Extract team_id from authenticated user context
    3. Delegate all operations to the selected backend
    """
    
    def __init__(self, db: Session, user_context: Optional[dict] = None):
        """Initialize token storage service.
        
        Args:
            db: Database session
            user_context: Authenticated user context (contains email, teams, etc.)
                         If None, team_id will be extracted from request context
        """
        self.db = db
        self.user_context = user_context
        settings = get_settings()
        
        if settings.oauth_token_backend == "vault":
            self._backend: AbstractTokenBackend = VaultTokenBackend(db, settings)
        elif settings.oauth_token_backend == "database":
            self._backend = DatabaseTokenBackend(db, settings)
        else:
            raise ValueError(
                f"Unknown OAUTH_TOKEN_BACKEND: {settings.oauth_token_backend}. "
                f"Expected 'database' or 'vault'."
            )

    def _get_team_id(self, app_user_email: str) -> str:
        """Extract team_id from authenticated user context.
        
        For Vault backend: team_id is required for path construction
        For Database backend: team_id is optional (DB schema doesn't have it yet)
        
        Args:
            app_user_email: User email for context lookup
            
        Returns:
            Team identifier string (e.g., "engineering", "sales")
            Falls back to "default" if no team context available
        """
        if self.user_context:
            teams = get_user_teams(self.user_context)
            return teams[0] if teams else "default"
        
        # Fallback: extract from current request context
        # This would be implemented based on your auth system
        # For example, from JWT claims or session data
        return "default"

    async def store_tokens(
        self,
        gateway_id: str,
        user_id: str,
        app_user_email: str,
        access_token: str,
        refresh_token: Optional[str],
        expires_in: Optional[int],
        scopes: list[str],
    ):
        """Store OAuth tokens for a user."""
        team_id = self._get_team_id(app_user_email)
        return await self._backend.store_tokens(
            gateway_id=gateway_id,
            team_id=team_id,
            user_id=user_id,
            app_user_email=app_user_email,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            scopes=scopes,
        )

    async def get_user_token(
        self,
        gateway_id: str,
        app_user_email: str,
        threshold_seconds: int = 300,
    ) -> Optional[str]:
        """Retrieve access token for a user."""
        team_id = self._get_team_id(app_user_email)
        return await self._backend.get_user_token(
            gateway_id=gateway_id,
            team_id=team_id,
            app_user_email=app_user_email,
            threshold_seconds=threshold_seconds,
        )

    async def get_token_info(
        self,
        gateway_id: str,
        app_user_email: str,
    ) -> Optional[dict]:
        """Get non-sensitive token metadata."""
        team_id = self._get_team_id(app_user_email)
        return await self._backend.get_token_info(
            gateway_id=gateway_id,
            team_id=team_id,
            app_user_email=app_user_email,
        )

    async def revoke_user_tokens(
        self,
        gateway_id: str,
        app_user_email: str,
    ) -> bool:
        """Delete stored tokens for a user."""
        team_id = self._get_team_id(app_user_email)
        return await self._backend.revoke_user_tokens(
            gateway_id=gateway_id,
            team_id=team_id,
            app_user_email=app_user_email,
        )

    async def cleanup_expired_tokens(self, max_age_days: int = 30) -> int:
        """Clean up expired tokens."""
        return await self._backend.cleanup_expired_tokens(max_age_days=max_age_days)
```

### 3. Key Design Decisions

#### 3.1 Team ID Extraction Strategy

The `_get_team_id()` method should follow this precedence:

1. **JWT claims** (`teams` array, take first team)
2. **Session data** (from `users` table or session store)
3. **Fallback to "default"** if no team context is available

This matches the existing RBAC team scoping documented in `CLAUDE.md`:

```python
def get_user_teams(user_context: dict) -> list[str]:
    """Extract team list from user context (JWT or session)."""
    # Check JWT claims first
    if "teams" in user_context:
        teams = user_context["teams"]
        if teams is None:  # Admin bypass
            return []
        if isinstance(teams, list) and teams:
            return teams
    
    # Check session user record
    if "user" in user_context:
        user = user_context["user"]
        if hasattr(user, "teams") and user.teams:
            return user.teams
    
    return ["default"]  # Fallback
```

#### 3.2 Database Backend Compatibility

The `DatabaseTokenBackend` should **ignore** the `team_id` parameter initially, since the `oauth_tokens` table doesn't have a `team_id` column yet. This is explicitly out of scope per the design document:

> **Database Changes Deferred:** Any modifications to the existing database backend (including `team_id` field addition, refactoring into `DatabaseTokenBackend`, or interface alignment) are **out of scope** for this phase.

```python
class DatabaseTokenBackend(AbstractTokenBackend):
    async def store_tokens(
        self,
        gateway_id: str,
        team_id: str,  # Received but not stored in DB yet
        user_id: str,
        ...
    ):
        # Ignore team_id parameter — oauth_tokens table doesn't have this column
        # Just use gateway_id as before
        ...
```

#### 3.3 Vault Backend Implementation

The `VaultTokenBackend` uses all three path segments:

```python
class VaultTokenBackend(AbstractTokenBackend):
    def _resolve_mcp_url(self, gateway_id: str) -> str:
        """Resolve gateway_id → gateways.url for Vault path construction."""
        gateway = self.db.get(Gateway, gateway_id)
        if not gateway:
            raise ValueError(f"Gateway {gateway_id} not found")
        return gateway.url

    def _hash_server_id(self, mcp_url: str) -> str:
        """Map mcp_url to stable path segment (hash or gateway UUID)."""
        import hashlib
        return hashlib.sha256(mcp_url.encode()).hexdigest()[:8]

    async def store_tokens(
        self,
        gateway_id: str,
        team_id: str,
        user_id: str,
        app_user_email: str,
        ...
    ):
        mcp_url = self._resolve_mcp_url(gateway_id)
        server_id = self._hash_server_id(mcp_url)
        
        # Construct Vault path with all three segments
        from urllib.parse import quote
        path = f"{self.mount}/data/{self.prefix}/{team_id}/{server_id}/{quote(app_user_email)}"
        
        # Write to Vault KV v2
        ...
```

## Summary

| Component | Change Required | Priority |
|-----------|----------------|----------|
| `TokenRecord` dataclass | Add `team_id` field | **HIGH** |
| `AbstractTokenBackend` interface | Add `team_id` parameter to all 5 methods | **HIGH** |
| `TokenStorageService` façade | Add `_get_team_id()` helper and pass to backend | **HIGH** |
| `DatabaseTokenBackend` | Accept `team_id` but don't use it (no DB column) | **MEDIUM** |
| `VaultTokenBackend` | Use `team_id` in path construction | **HIGH** |

These changes are **required** for Vault integration to work correctly. Without `team_id`, the Vault backend cannot construct the required path format.
