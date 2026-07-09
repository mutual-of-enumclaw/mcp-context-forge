"""
Vault token storage backend.

Stores OAuth tokens in HashiCorp Vault KV v2 using httpx for HTTP API calls.
Path structure: {mount}/data/{prefix}/{team_id}/{server_id}/{url-encoded-email}
where server_id is SHA-256 hash of gateways.url (mcp_url).
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx
from sqlalchemy.orm import Session

from mcpgateway.common.validators import SecurityValidator
from mcpgateway.config import Settings
from mcpgateway.db import Gateway
from mcpgateway.services.oauth_manager import OAuthManager, parse_expires_in

from .base import AbstractTokenBackend, TokenRecord

logger = logging.getLogger(__name__)


class VaultConnectionError(Exception):
    """Raised when Vault is unreachable or returns server errors."""


class VaultAuthError(Exception):
    """Raised when Vault authentication fails (403)."""


class VaultTokenBackend(AbstractTokenBackend):
    """
    Vault KV v2 token storage backend.

    Features:
    - Resolves gateway_id → gateways.url → server_id (SHA-256 hash)
    - Constructs path: {mount}/data/{prefix}/{team_id}/{server_id}/{url-encoded-email}
    - Stores tokens plain-text in Vault (Vault encrypts at rest)
    - Retry logic (3 attempts with exponential backoff)
    - Optional in-memory token cache with TTL
    """

    def __init__(self, db: Session, settings: Settings):
        """Initialize Vault backend.

        Args:
            db: SQLAlchemy session (for gateway_id → gateways.url resolution)
            settings: Application settings
        """
        self.db = db
        self.settings = settings

        # Vault connection
        self.vault_addr = settings.vault_addr
        self.vault_token = settings.vault_token.get_secret_value() if settings.vault_token else None
        self.vault_namespace = settings.vault_namespace or None
        self.mount = settings.vault_kv_mount
        self.prefix = settings.vault_kv_path_prefix
        self.tls_verify = settings.vault_tls_verify

        if not self.vault_token:
            raise ValueError("VAULT_TOKEN is required when OAUTH_TOKEN_BACKEND=vault")

        # Optional in-memory cache
        self.cache_enabled = settings.vault_token_cache_enabled
        if self.cache_enabled:
            self._cache: dict[tuple[str, str, str], dict[str, Any]] = {}  # (team_id, server_id, email) → {token, expires}
            self.cache_ttl = settings.vault_token_cache_ttl
            self.cache_max_size = settings.vault_token_cache_max_size

    def _resolve_mcp_url(self, gateway_id: str) -> str:
        """Resolve gateway_id → gateways.url.

        This is the key difference from DB backend: Vault uses mcp_url (gateways.url)
        as the credential anchor, not gateway_id.

        Args:
            gateway_id: Gateway UUID

        Returns:
            Gateway URL (mcp_url)

        Raises:
            ValueError: If gateway not found
        """
        gateway = self.db.get(Gateway, gateway_id)
        if not gateway:
            raise ValueError(f"Gateway {gateway_id} not found")
        return gateway.url

    def _hash_server_id(self, mcp_url: str) -> str:
        """Hash mcp_url to stable server_id (first 8 hex chars of SHA-256).

        Args:
            mcp_url: Gateway URL (e.g., https://mcp.github.acme.com)

        Returns:
            8-character hex string
        """
        return hashlib.sha256(mcp_url.encode()).hexdigest()[:8]

    def _construct_vault_path(self, team_id: str, mcp_url: str, app_user_email: str) -> str:
        """Construct full Vault KV v2 path.

        Args:
            team_id: Team identifier
            mcp_url: Gateway URL (will be hashed to server_id)
            app_user_email: User email (will be URL-encoded)

        Returns:
            Full Vault path (e.g., secret/data/contextforge/oauth/engineering/647ad7b3/alice%40example.com)
        """
        server_id = self._hash_server_id(mcp_url)
        email_encoded = quote(app_user_email, safe="")
        return f"{self.mount}/data/{self.prefix}/{team_id}/{server_id}/{email_encoded}"

    def _construct_metadata_path(self, team_id: str, mcp_url: str, app_user_email: str) -> str:
        """Construct Vault KV v2 metadata path (for hard delete).

        Args:
            team_id: Team identifier
            mcp_url: Gateway URL
            app_user_email: User email

        Returns:
            Metadata path (e.g., secret/metadata/contextforge/oauth/engineering/647ad7b3/alice%40example.com)
        """
        server_id = self._hash_server_id(mcp_url)
        email_encoded = quote(app_user_email, safe="")
        return f"{self.mount}/metadata/{self.prefix}/{team_id}/{server_id}/{email_encoded}"

    async def _vault_request(self, method: str, path: str, data: dict | None = None) -> dict | None:
        """Make HTTP request to Vault with retry logic.

        Args:
            method: HTTP method (GET, POST, DELETE)
            path: Vault API path (relative to /v1/)
            data: Request body (for POST)

        Returns:
            JSON response or None if 404

        Raises:
            VaultConnectionError: If Vault unreachable after retries
            VaultAuthError: If authentication fails (403)
        """
        headers = {"X-Vault-Token": self.vault_token}
        if self.vault_namespace:
            headers["X-Vault-Namespace"] = self.vault_namespace

        url = f"{self.vault_addr}/v1/{path}"

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(verify=self.tls_verify, timeout=10.0) as client:
                    if method == "GET":
                        resp = await client.get(url, headers=headers)
                    elif method == "POST":
                        resp = await client.post(url, headers=headers, json=data)
                    elif method == "DELETE":
                        resp = await client.delete(url, headers=headers)
                    else:
                        raise ValueError(f"Unsupported HTTP method: {method}")

                    # Handle 404 as "not found" (expected for missing tokens)
                    if resp.status_code == 404:
                        return None

                    # Raise for other errors
                    resp.raise_for_status()

                    # Return JSON response (or empty dict for DELETE)
                    return resp.json() if resp.content else {}

            except httpx.ConnectTimeout as e:
                if attempt < 2:
                    await asyncio.sleep(2**attempt)  # 1s, 2s
                    continue
                logger.error(
                    "Vault unreachable after 3 attempts: %s",
                    SecurityValidator.sanitize_log_message(url),
                )
                raise VaultConnectionError("Credential storage unavailable") from e

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    logger.critical("Vault auth failure - VAULT_TOKEN invalid or expired")
                    raise VaultAuthError("VAULT_TOKEN invalid or expired") from e
                # Re-raise other HTTP errors
                raise

        # Should never reach here due to raise in loop, but make mypy happy
        raise VaultConnectionError("Unexpected error in Vault request retry logic")

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
        """Store OAuth tokens in Vault.

        Args:
            gateway_id: Gateway ID (resolved to mcp_url)
            team_id: Team identifier (used in Vault path)
            user_id: OAuth provider user ID
            app_user_email: ContextForge user email
            access_token: Access token (stored plain-text in Vault)
            refresh_token: Refresh token (stored plain-text in Vault)
            expires_in: Token expiration in seconds, or None
            scopes: OAuth scopes

        Returns:
            TokenRecord with plain-text tokens
        """
        mcp_url = self._resolve_mcp_url(gateway_id)
        path = self._construct_vault_path(team_id, mcp_url, app_user_email)

        # Calculate expiration
        expires_at = None
        if expires_in is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        now = datetime.now(timezone.utc)

        # Build payload (nested token object for cleaner structure)
        payload = {
            "data": {
                "email": app_user_email,
                "team_id": team_id,
                "mcp_url": mcp_url,  # ← Key difference: store mcp_url, not gateway_id
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

        # Write to Vault
        await self._vault_request("POST", path, payload)

        # Invalidate cache
        if self.cache_enabled:
            server_id = self._hash_server_id(mcp_url)
            cache_key = (team_id, server_id, app_user_email)
            self._cache.pop(cache_key, None)

        logger.info(
            "Stored OAuth tokens in Vault for gateway %s (mcp_url=%s), team=%s, user=%s",
            SecurityValidator.sanitize_log_message(gateway_id),
            SecurityValidator.sanitize_log_message(mcp_url),
            SecurityValidator.sanitize_log_message(team_id),
            SecurityValidator.sanitize_log_message(app_user_email),
        )

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
        """Get valid access token from Vault, refreshing if necessary.

        Args:
            gateway_id: Gateway ID (resolved to mcp_url)
            team_id: Team identifier
            app_user_email: ContextForge user email
            threshold_seconds: Seconds before expiry to consider token expired

        Returns:
            Plain-text access token or None
        """
        mcp_url = self._resolve_mcp_url(gateway_id)
        server_id = self._hash_server_id(mcp_url)
        cache_key = (team_id, server_id, app_user_email)

        # Check cache first
        if self.cache_enabled and cache_key in self._cache:
            cached = self._cache[cache_key]
            if datetime.now(timezone.utc) < cached["cache_expires"]:
                logger.debug("Cache hit for token: team=%s, server_id=%s, email=%s", team_id, server_id, app_user_email)
                return cached["token"]

        # Fetch from Vault
        path = self._construct_vault_path(team_id, mcp_url, app_user_email)
        result = await self._vault_request("GET", path)

        if not result or "data" not in result:
            logger.debug(
                "No OAuth tokens found in Vault for gateway %s (mcp_url=%s), team=%s, user=%s",
                SecurityValidator.sanitize_log_message(gateway_id),
                SecurityValidator.sanitize_log_message(mcp_url),
                SecurityValidator.sanitize_log_message(team_id),
                SecurityValidator.sanitize_log_message(app_user_email),
            )
            return None

        data = result["data"]["data"]
        access_token = data["token"]["access_token"]
        refresh_token = data["token"].get("refresh_token")
        expires_at_str = data.get("expires_at")

        # Check expiry and refresh if needed
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            if (expires_at - datetime.now(timezone.utc)).total_seconds() < threshold_seconds:
                logger.info(
                    "OAuth token near expiry for gateway %s, team=%s, user=%s",
                    SecurityValidator.sanitize_log_message(gateway_id),
                    SecurityValidator.sanitize_log_message(team_id),
                    SecurityValidator.sanitize_log_message(app_user_email),
                )
                if refresh_token:
                    new_token = await self._refresh_access_token(gateway_id, team_id, app_user_email, refresh_token, data)
                    if new_token:
                        return new_token
                return None  # Expired, no refresh available

        # Cache token
        if self.cache_enabled:
            self._cache[cache_key] = {
                "token": access_token,
                "cache_expires": datetime.now(timezone.utc) + timedelta(seconds=self.cache_ttl),
            }
            # Simple LRU eviction if cache too large
            if len(self._cache) > self.cache_max_size:
                # Remove oldest entry (first key)
                oldest_key = next(iter(self._cache))
                self._cache.pop(oldest_key)

        return access_token

    async def get_token_info(
        self,
        gateway_id: str,
        team_id: str,
        app_user_email: str,
    ) -> dict | None:
        """Get non-sensitive token metadata from Vault.

        Args:
            gateway_id: Gateway ID
            team_id: Team identifier
            app_user_email: ContextForge user email

        Returns:
            Token info dict or None
        """
        mcp_url = self._resolve_mcp_url(gateway_id)
        path = self._construct_vault_path(team_id, mcp_url, app_user_email)
        result = await self._vault_request("GET", path)

        if not result or "data" not in result:
            return None

        data = result["data"]["data"]
        expires_at_str = data.get("expires_at")
        updated_at_str = data.get("updated_at")

        # Determine status
        status = "valid"
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if expires_at <= now:
                status = "expired"
            elif (expires_at - now).total_seconds() < 300:
                status = "near_expiry"

        return {
            "scopes": data["token"]["scopes"],
            "expires_at": expires_at_str,
            "status": status,
            "updated_at": updated_at_str,
        }

    async def revoke_user_tokens(
        self,
        gateway_id: str,
        team_id: str,
        app_user_email: str,
    ) -> bool:
        """Delete tokens from Vault (hard delete via metadata endpoint).

        Args:
            gateway_id: Gateway ID
            team_id: Team identifier
            app_user_email: ContextForge user email

        Returns:
            True if deleted, False if not found
        """
        mcp_url = self._resolve_mcp_url(gateway_id)
        metadata_path = self._construct_metadata_path(team_id, mcp_url, app_user_email)

        try:
            result = await self._vault_request("DELETE", metadata_path)

            # Invalidate cache
            if self.cache_enabled:
                server_id = self._hash_server_id(mcp_url)
                cache_key = (team_id, server_id, app_user_email)
                self._cache.pop(cache_key, None)

            logger.info(
                "Revoked OAuth tokens in Vault for gateway %s (mcp_url=%s), team=%s, user=%s",
                SecurityValidator.sanitize_log_message(gateway_id),
                SecurityValidator.sanitize_log_message(mcp_url),
                SecurityValidator.sanitize_log_message(team_id),
                SecurityValidator.sanitize_log_message(app_user_email),
            )
            return result is not None  # None = 404 (not found)

        except Exception as e:
            logger.error("Failed to revoke OAuth tokens in Vault: %s", str(e))
            return False

    async def cleanup_expired_tokens(
        self,
        max_age_days: int = 30,
    ) -> int:
        """No-op for Vault backend.

        Vault KV TTL or operator-configured cleanup policies handle expiration.
        Log a warning once per process start to educate operators.

        Args:
            max_age_days: Ignored (for interface compatibility)

        Returns:
            0 (no tokens cleaned)
        """
        logger.warning(
            "cleanup_expired_tokens is a no-op for Vault backend. "
            "Configure Vault KV TTL or retention policies to handle cleanup. "
            "max_age_days=%d parameter ignored.",
            max_age_days,
        )
        return 0

    # ──────────────────────────────────────────────────────────────────────
    # Private helper methods
    # ──────────────────────────────────────────────────────────────────────

    async def _refresh_access_token(
        self,
        gateway_id: str,
        team_id: str,
        app_user_email: str,
        refresh_token: str,
        vault_data: dict,
    ) -> str | None:
        """Refresh an expired access token using refresh token.

        Args:
            gateway_id: Gateway ID
            team_id: Team identifier
            app_user_email: ContextForge user email
            refresh_token: Plain-text refresh token
            vault_data: Current Vault token data (for preserving metadata)

        Returns:
            New access token or None if refresh failed
        """
        try:
            # Get the gateway configuration
            gateway = self.db.query(Gateway).filter(Gateway.id == gateway_id).first()

            if not gateway or not gateway.oauth_config:
                logger.error("No OAuth configuration found for gateway %s", gateway_id)
                return None

            # PR #4341: Refuse refresh on private gateway whose owner != token owner
            gateway_visibility = getattr(gateway, "visibility", "public")
            gateway_owner_email = getattr(gateway, "owner_email", None)
            if gateway_visibility == "private" and gateway_owner_email and gateway_owner_email != app_user_email:
                logger.warning(
                    "OAuth refresh denied: gateway %s is private and owned by %s, not token owner %s",
                    gateway_id,
                    gateway_owner_email,
                    app_user_email,
                )
                return None

            oauth_config = gateway.oauth_config.copy()

            # RFC 8707: Set resource parameter
            def normalize_resource(url: str, *, preserve_query: bool = False) -> str | None:
                """Normalize a resource value per RFC 8707."""
                if not url:
                    return None
                parsed = urlparse(url)
                if not parsed.scheme:
                    return url  # Opaque identifier
                query = parsed.query if preserve_query else ""
                return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))

            existing_resource = oauth_config.get("resource")
            if existing_resource:
                if isinstance(existing_resource, list):
                    normalized = [normalize_resource(r, preserve_query=True) for r in existing_resource]
                    oauth_config["resource"] = [r for r in normalized if r]
                else:
                    oauth_config["resource"] = normalize_resource(existing_resource, preserve_query=True)
            elif gateway.url:
                oauth_config["resource"] = normalize_resource(gateway.url)

            # Use OAuthManager to refresh the token
            oauth_manager = OAuthManager()

            logger.info("Attempting to refresh token in Vault for gateway %s, user %s", gateway_id, app_user_email)
            token_response = await oauth_manager.refresh_token(
                refresh_token,
                oauth_config,
                ca_certificate=gateway.ca_certificate,
                client_cert=gateway.client_cert,
                client_key=gateway.client_key,
            )

            # Extract new tokens
            new_access_token = token_response["access_token"]
            new_refresh_token = token_response.get("refresh_token", refresh_token)
            expires_in = parse_expires_in(token_response)

            # Store refreshed tokens back to Vault
            await self.store_tokens(
                gateway_id=gateway_id,
                team_id=team_id,
                user_id=vault_data.get("user_id", ""),
                app_user_email=app_user_email,
                access_token=new_access_token,
                refresh_token=new_refresh_token,
                expires_in=expires_in,
                scopes=vault_data["token"]["scopes"],
            )

            logger.info("Successfully refreshed token in Vault for gateway %s, user %s", gateway_id, app_user_email)

            return new_access_token

        except Exception as e:
            logger.error("Failed to refresh OAuth token in Vault for gateway %s: %s", gateway_id, str(e))
            # If refresh fails with invalid/expired error, delete tokens
            if "invalid" in str(e).lower() or "expired" in str(e).lower():
                logger.warning("Refresh token appears invalid/expired, deleting tokens in Vault for gateway %s", gateway_id)
                await self.revoke_user_tokens(gateway_id, team_id, app_user_email)
            return None
