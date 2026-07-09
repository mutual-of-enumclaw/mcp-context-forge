# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_token_storage_service.py
Copyright 2026
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

Unit tests for TokenStorageService façade.
"""

# Standard
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

# Third-Party
from pydantic import SecretStr
import pytest

# First-Party
from mcpgateway.services.token_storage_service import TokenStorageService


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    db = MagicMock()
    return db


@pytest.fixture
def mock_settings_database():
    """Create mock settings for database backend."""
    settings = MagicMock()
    settings.auth_encryption_secret = "test-salt"  # pragma: allowlist secret
    settings.oauth_token_backend = "database"
    return settings


@pytest.fixture
def mock_settings_vault():
    """Create mock settings for Vault backend."""
    settings = MagicMock()
    settings.oauth_token_backend = "vault"
    settings.vault_addr = "https://vault.example.com"
    settings.vault_token = SecretStr("test-token")  # pragma: allowlist secret
    settings.vault_mount_point = "secret"
    return settings


# ---------- Initialization Tests ----------


def test_init_database_backend(mock_db, mock_settings_database):
    """Test TokenStorageService initializes with database backend."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service") as mock_enc:
            mock_enc.return_value = MagicMock()
            service = TokenStorageService(mock_db)

            assert service.db == mock_db
            assert service._backend is not None
            # Verify it's a DatabaseTokenBackend
            from mcpgateway.services.token_backends.db_backend import DatabaseTokenBackend
            assert isinstance(service._backend, DatabaseTokenBackend)


def test_init_vault_backend(mock_db, mock_settings_vault):
    """Test TokenStorageService initializes with Vault backend."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_vault

        # VaultTokenBackend uses httpx, not hvac
        service = TokenStorageService(mock_db)

        assert service.db == mock_db
        assert service._backend is not None
        # Verify it's a VaultTokenBackend
        from mcpgateway.services.token_backends.vault_backend import VaultTokenBackend
        assert isinstance(service._backend, VaultTokenBackend)


def test_init_invalid_backend(mock_db):
    """Test TokenStorageService raises error for invalid backend."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.oauth_token_backend = "invalid"
        mock_get_settings.return_value = mock_settings

        with pytest.raises(ValueError, match="Unknown OAUTH_TOKEN_BACKEND"):
            TokenStorageService(mock_db)


def test_init_with_user_context(mock_db, mock_settings_database):
    """Test TokenStorageService accepts user_context parameter."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            user_context = {'email': 'user@example.com', 'teams': ['engineering']}
            service = TokenStorageService(mock_db, user_context=user_context)

            assert service.user_context == user_context


# ---------- Team ID Extraction Tests ----------


def test_get_team_id_from_teams_list(mock_db, mock_settings_database):
    """Test _get_team_id extracts first team from teams list."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            user_context = {'email': 'user@example.com', 'teams': ['engineering', 'admin']}
            service = TokenStorageService(mock_db, user_context=user_context)

            team_id = service._get_team_id('user@example.com')
            assert team_id == 'engineering'


def test_get_team_id_empty_teams(mock_db, mock_settings_database):
    """Test _get_team_id queries database when user_context has empty teams."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            user_context = {'email': 'user@example.com', 'teams': []}
            service = TokenStorageService(mock_db, user_context=user_context)

            # Mock database query to return empty list (no team memberships)
            scalars_mock = Mock()
            scalars_mock.all = Mock(return_value=[])
            execute_result = Mock()
            execute_result.scalars = Mock(return_value=scalars_mock)
            mock_db.execute = Mock(return_value=execute_result)

            team_id = service._get_team_id('user@example.com')
            assert team_id == 'default'


def test_get_team_id_no_user_context(mock_db, mock_settings_database):
    """Test _get_team_id queries database when no user_context."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            service = TokenStorageService(mock_db)

            # Mock database query to return empty list (no team memberships)
            scalars_mock = Mock()
            scalars_mock.all = Mock(return_value=[])
            execute_result = Mock()
            execute_result.scalars = Mock(return_value=scalars_mock)
            mock_db.execute = Mock(return_value=execute_result)

            team_id = service._get_team_id('user@example.com')
            assert team_id == 'default'


# ---------- Façade Method Delegation Tests ----------


@pytest.mark.asyncio
async def test_store_tokens_delegates_to_backend(mock_db, mock_settings_database):
    """Test store_tokens delegates to backend with team_id."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            user_context = {'email': 'user@example.com', 'teams': ['engineering']}
            service = TokenStorageService(mock_db, user_context=user_context)

            # Mock the backend's store_tokens method
            service._backend.store_tokens = AsyncMock(return_value=MagicMock(
                gateway_id="gw-1",
                team_id="engineering",
                user_id="oauth-user-1",
                app_user_email="user@example.com",
                mcp_url="https://example.com"
            ))

            result = await service.store_tokens(
                gateway_id="gw-1",
                user_id="oauth-user-1",
                app_user_email="user@example.com",
                access_token="access",
                refresh_token="refresh",
                expires_in=3600,
                scopes=["read"],
            )

            # Verify backend was called with team_id
            service._backend.store_tokens.assert_called_once_with(
                gateway_id="gw-1",
                team_id="engineering",
                user_id="oauth-user-1",
                app_user_email="user@example.com",
                access_token="access",
                refresh_token="refresh",
                expires_in=3600,
                scopes=["read"],
            )

            assert result.gateway_id == "gw-1"
            assert result.team_id == "engineering"


@pytest.mark.asyncio
async def test_get_user_token_delegates_to_backend(mock_db, mock_settings_database):
    """Test get_user_token delegates to backend with team_id."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            user_context = {'email': 'user@example.com', 'teams': ['engineering']}
            service = TokenStorageService(mock_db, user_context=user_context)

            # Mock the backend's get_user_token method
            service._backend.get_user_token = AsyncMock(return_value="decrypted_token")

            result = await service.get_user_token(
                gateway_id="gw-1",
                app_user_email="user@example.com",
                threshold_seconds=300,
            )

            # Verify backend was called with team_id
            service._backend.get_user_token.assert_called_once_with(
                gateway_id="gw-1",
                team_id="engineering",
                app_user_email="user@example.com",
                threshold_seconds=300,
            )

            assert result == "decrypted_token"


@pytest.mark.asyncio
async def test_get_token_info_delegates_to_backend(mock_db, mock_settings_database):
    """Test get_token_info delegates to backend with team_id."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            user_context = {'email': 'user@example.com', 'teams': ['engineering']}
            service = TokenStorageService(mock_db, user_context=user_context)

            # Mock the backend's get_token_info method
            service._backend.get_token_info = AsyncMock(return_value={
                "user_id": "oauth-user-1",
                "app_user_email": "user@example.com",
                "token_type": "bearer",
                "expires_at": None,
                "scopes": ["read"],
            })

            result = await service.get_token_info(
                gateway_id="gw-1",
                app_user_email="user@example.com",
            )

            # Verify backend was called with team_id
            service._backend.get_token_info.assert_called_once_with(
                gateway_id="gw-1",
                team_id="engineering",
                app_user_email="user@example.com",
            )

            assert result["user_id"] == "oauth-user-1"


@pytest.mark.asyncio
async def test_revoke_user_tokens_delegates_to_backend(mock_db, mock_settings_database):
    """Test revoke_user_tokens delegates to backend with team_id."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            user_context = {'email': 'user@example.com', 'teams': ['engineering']}
            service = TokenStorageService(mock_db, user_context=user_context)

            # Mock the backend's revoke_user_tokens method
            service._backend.revoke_user_tokens = AsyncMock(return_value=True)

            result = await service.revoke_user_tokens(
                gateway_id="gw-1",
                app_user_email="user@example.com",
            )

            # Verify backend was called with team_id
            service._backend.revoke_user_tokens.assert_called_once_with(
                gateway_id="gw-1",
                team_id="engineering",
                app_user_email="user@example.com",
            )

            assert result is True


@pytest.mark.asyncio
async def test_cleanup_expired_tokens_delegates_to_backend(mock_db, mock_settings_database):
    """Test cleanup_expired_tokens delegates to backend."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            service = TokenStorageService(mock_db)

            # Mock the backend's cleanup_expired_tokens method
            service._backend.cleanup_expired_tokens = AsyncMock(return_value=5)

            result = await service.cleanup_expired_tokens(max_age_days=30)

            # Verify backend was called
            service._backend.cleanup_expired_tokens.assert_called_once_with(max_age_days=30)

            assert result == 5


def test_get_team_id_with_user_context(mock_db, mock_settings_database):
    """Test _get_team_id uses user_context when available."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            user_context = {"teams": ["engineering", "platform"]}
            service = TokenStorageService(mock_db, user_context=user_context)

            team_id = service._get_team_id("user@example.com")

            # Should return first team from user_context
            assert team_id == "engineering"


def test_get_team_id_queries_database_when_context_empty(mock_db, mock_settings_database):
    """Test _get_team_id queries database when user_context is empty (OAuth callback scenario)."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            # Empty user_context (simulates OAuth callback without authentication)
            service = TokenStorageService(mock_db, user_context={})

            # Mock database query to return team memberships
            from mcpgateway.db import EmailTeamMember
            team_member = Mock(spec=EmailTeamMember)
            team_member.team_id = "marketing"
            team_member.user_email = "user@example.com"
            team_member.is_active = True

            scalars_mock = Mock()
            scalars_mock.all = Mock(return_value=[team_member])
            execute_result = Mock()
            execute_result.scalars = Mock(return_value=scalars_mock)
            mock_db.execute = Mock(return_value=execute_result)

            team_id = service._get_team_id("user@example.com")

            # Should query database and return team from EmailTeamMember
            assert team_id == "marketing"
            mock_db.execute.assert_called_once()


def test_get_team_id_falls_back_to_default_when_no_teams(mock_db, mock_settings_database):
    """Test _get_team_id returns 'default' when user has no team memberships."""
    with patch("mcpgateway.services.token_storage_service.get_settings") as mock_get_settings:
        mock_get_settings.return_value = mock_settings_database

        with patch("mcpgateway.services.token_backends.db_backend.get_encryption_service"):
            # Empty user_context
            service = TokenStorageService(mock_db, user_context={})

            # Mock database query to return empty list (no team memberships)
            scalars_mock = Mock()
            scalars_mock.all = Mock(return_value=[])
            execute_result = Mock()
            execute_result.scalars = Mock(return_value=scalars_mock)
            mock_db.execute = Mock(return_value=execute_result)

            team_id = service._get_team_id("user@example.com")

            # Should fall back to "default"
            assert team_id == "default"
