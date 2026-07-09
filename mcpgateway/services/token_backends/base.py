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

    gateway_id: str  # gateways.id (UUID) - used by DB backend as FK
    mcp_url: str  # gateways.url - resolved by VaultTokenBackend; Vault path key
    team_id: str  # Team identifier - extracted from user context
    user_id: str  # OAuth provider user ID (e.g., GitHub numeric UID)
    app_user_email: str  # ContextForge user identity
    access_token: str  # Plain-text (backends handle encryption differently)
    refresh_token: str | None  # Nullable
    token_type: str  # Always "Bearer"
    expires_at: datetime | None  # Nullable (some providers omit expiry)
    scopes: list[str]  # OAuth scopes
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
        gateway_id: str,  # UUID from gateways.id - passed by all existing call sites
        team_id: str,  # Team identifier from user context (JWT/session)
        user_id: str,  # OAuth provider user ID
        app_user_email: str,  # ContextForge user email
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
        team_id: str,  # Team identifier from user context
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
        team_id: str,  # Team identifier from user context
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
        team_id: str,  # Team identifier from user context
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
