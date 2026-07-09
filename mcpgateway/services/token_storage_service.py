# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/token_storage_service.py
Copyright 2026
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

OAuth Token Storage Service for ContextForge - Façade Pattern.

This module provides a unified interface for token storage, delegating to
pluggable backends (database or Vault) based on configuration.

Phase 1: Minimal façade implementation with backend selection.
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from mcpgateway.config import get_settings

# Import backends
from mcpgateway.services.token_backends import (
    AbstractTokenBackend,
    DatabaseTokenBackend,
    TokenRecord,
    VaultTokenBackend,
)

logger = logging.getLogger(__name__)


class TokenStorageService:
    """
    Façade for OAuth token storage with pluggable backends.

    Selects backend based on OAUTH_TOKEN_BACKEND environment variable:
    - 'database' (default): DatabaseTokenBackend (existing behavior)
    - 'vault': VaultTokenBackend (stores in HashiCorp Vault)

    Extracts team_id from user_context and passes to backend along with gateway_id.
    Public method signatures remain unchanged for backward compatibility.

    Examples:
        >>> from unittest.mock import MagicMock
        >>> service = TokenStorageService(MagicMock(), user_context={'email': 'user@example.com', 'teams': ['engineering']})
        >>> service._get_team_id('user@example.com')
        'engineering'
        >>> service2 = TokenStorageService(MagicMock(), user_context={'email': 'user@example.com'})
        >>> service2._get_team_id('user@example.com')
        'default'
    """

    def __init__(self, db: Session, user_context: Optional[Dict[str, Any]] = None):
        """Initialize token storage service with selected backend.

        Args:
            db: SQLAlchemy session (used by both backends for different purposes:
                DatabaseTokenBackend uses it for token CRUD operations,
                VaultTokenBackend uses it for gateway_id → gateways.url resolution)
            user_context: JWT claims or session data for team_id extraction.
                Expected keys: 'email', 'teams' (list), 'is_admin' (bool)

        Raises:
            ValueError: If OAUTH_TOKEN_BACKEND has unknown value
        """
        self.db = db
        self.user_context = user_context or {}
        settings = get_settings()

        # Select backend based on configuration
        if settings.oauth_token_backend == "vault":
            self._backend: AbstractTokenBackend = VaultTokenBackend(db, settings)
            logger.info("Token storage backend: Vault (addr=%s)", settings.vault_addr)
        elif settings.oauth_token_backend == "database":
            self._backend = DatabaseTokenBackend(db, settings)
            logger.debug("Token storage backend: Database")
        else:
            raise ValueError(
                f"Unknown OAUTH_TOKEN_BACKEND: {settings.oauth_token_backend}. "
                f"Expected 'database' or 'vault'."
            )

    def _get_team_id(self, app_user_email: str) -> str:
        """
        Extract team_id from authenticated user context.

        Precedence: JWT 'teams' claim → session 'teams' → fallback 'default'.

        Args:
            app_user_email: User email (unused in current implementation, for future expansion)

        Returns:
            Team identifier string (first team if multiple, or 'default')
        """
        if self.user_context:
            teams = self.user_context.get("teams", [])
            if isinstance(teams, list) and teams:
                return teams[0]  # Use first team
        return "default"

    async def store_tokens(
        self,
        gateway_id: str,
        user_id: str,
        app_user_email: str,
        access_token: str,
        refresh_token: Optional[str],
        expires_in: Optional[int],
        scopes: List[str],
    ) -> TokenRecord:
        """Store OAuth tokens for a gateway-user combination.

        Args:
            gateway_id: ID of the gateway
            user_id: OAuth provider user ID
            app_user_email: ContextForge user email (required)
            access_token: Access token from OAuth provider
            refresh_token: Refresh token from OAuth provider (optional)
            expires_in: Token expiration time in seconds, or None if the provider does not specify expiration
            scopes: List of OAuth scopes granted

        Returns:
            TokenRecord with token data

        Raises:
            OAuthError: If token storage fails
        """
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
        """Get a valid access token for a specific ContextForge user, refreshing if necessary.

        Args:
            gateway_id: ID of the gateway
            app_user_email: ContextForge user email (required)
            threshold_seconds: Seconds before expiry to consider token expired

        Returns:
            Valid access token or None if no valid token available for this user
        """
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
    ) -> Optional[Dict[str, Any]]:
        """Get information about stored OAuth tokens.

        Args:
            gateway_id: ID of the gateway
            app_user_email: ContextForge user email

        Returns:
            Token information dictionary or None if not found
        """
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
        """Revoke OAuth tokens for a specific user.

        Args:
            gateway_id: ID of the gateway
            app_user_email: ContextForge user email

        Returns:
            True if tokens were revoked successfully
        """
        team_id = self._get_team_id(app_user_email)
        return await self._backend.revoke_user_tokens(
            gateway_id=gateway_id,
            team_id=team_id,
            app_user_email=app_user_email,
        )

    async def cleanup_expired_tokens(
        self,
        max_age_days: int = 30,
    ) -> int:
        """Clean up expired/old tokens.

        DatabaseTokenBackend: Deletes expired rows from oauth_tokens table.
        VaultTokenBackend: Returns 0 (Vault KV TTL handles cleanup).

        Args:
            max_age_days: Maximum age of tokens to keep

        Returns:
            Number of tokens cleaned up (0 for Vault backend)
        """
        return await self._backend.cleanup_expired_tokens(max_age_days=max_age_days)
