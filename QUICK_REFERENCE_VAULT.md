# Quick Reference: Vault Token Storage Implementation

**Feature #5402 | Phase 1**

---

## 📋 What This Feature Does

Adds pluggable token storage to ContextForge with two backends:
1. **DatabaseTokenBackend** (default, existing behavior)
2. **VaultTokenBackend** (new, stores OAuth tokens in HashiCorp Vault)

**Key Design Principles:**
- Client never sees `gateway_id` or `team_id` - only uses virtual `server_id`
- Vault path uses `mcp_url` (not `gateway_id`) as the credential anchor
- Phase 1: NO database schema changes (minimal extraction only)
- Clean backend separation - no dual-mode fallback

---

## 🗂️ File Changes Overview

| File | Change Type | Description |
|------|-------------|-------------|
| `mcpgateway/services/token_backends/base.py` | NEW | AbstractTokenBackend + TokenRecord |
| `mcpgateway/services/token_backends/db_backend.py` | NEW | DatabaseTokenBackend (extracted) |
| `mcpgateway/services/token_backends/vault_backend.py` | NEW | VaultTokenBackend (full implementation) |
| `mcpgateway/services/token_storage_service.py` | REFACTOR | Becomes façade (backend selector) |
| `mcpgateway/routers/vault_router.py` | NEW | /vault/authorize + /vault/callback |
| `mcpgateway/config.py` | ADD | 10 new Vault env vars |
| `mcpgateway/main.py` | MODIFY | Register vault_router conditionally |
| `mcpgateway/routers/oauth_router.py` | MINIMAL | Pass user_context to service |
| `mcpgateway/services/tool_service.py` | MINIMAL | Pass user_context to service |
| `mcpgateway/services/gateway_service.py` | MINIMAL | Pass user_context to service |
| `mcpgateway/services/resource_service.py` | MINIMAL | Pass user_context to service |
| `mcpgateway/admin.py` | MINIMAL | Pass user_context to service |
| `pyproject.toml` | ADD | [vault] extra = hvac>=2.3.0 |

---

## 🔑 Key Concepts

### 1. Vault Path Structure
```
{VAULT_KV_MOUNT}/data/{VAULT_KV_PATH_PREFIX}/{team_id}/{server_id}/{url-encoded-email}

Example:
secret/data/contextforge/oauth/engineering/647ad7b3/alice%40acme.com
```

**Path derivation:**
- `team_id` - from JWT claims or session (fallback "default")
- `server_id` - SHA-256 hash of `gateways.url` (first 8 hex chars)
- `email` - URL-encoded user email

### 2. Resolution Chain: server_id → mcp_url
```
Client provides: server_id (from MCP config URL)
    ↓
Service resolves: server_id → server_tool_association → tools.gateway_id
    ↓
VaultTokenBackend resolves: gateway_id → gateways.url (the mcp_url)
    ↓
Hash: SHA-256(mcp_url)[:8] = server_id for Vault path
    ↓
Construct: secret/data/contextforge/oauth/{team_id}/{server_id}/{email}
```

### 3. Backend Selection
```python
OAUTH_TOKEN_BACKEND=database  → DatabaseTokenBackend
OAUTH_TOKEN_BACKEND=vault     → VaultTokenBackend
```

No fallback or dual-mode. Choose one at deployment time.

### 4. DatabaseTokenBackend in Phase 1
```python
# Accepts team_id but IGNORES it
async def store_tokens(self, gateway_id, team_id, ...):
    # team_id parameter exists but not used
    # Query: WHERE gateway_id=? AND app_user_email=?
    # NO team_id in WHERE clause
    ...
```

**Phase 2 will:**
- Add `team_id` column to `oauth_tokens` table
- Use `team_id` in SQL WHERE clauses
- Change unique constraint to `(team_id, gateway_id, app_user_email)`

---

## 🚀 Quick Start for Implementation

### Step 1: Create Base Interface
```bash
mkdir -p mcpgateway/services/token_backends
touch mcpgateway/services/token_backends/__init__.py
touch mcpgateway/services/token_backends/base.py
```

Copy interface from IMPLEMENTATION_SUMMARY_VAULT.md → base.py

### Step 2: Extract Database Backend
```bash
# Copy lines 119-620 from token_storage_service.py
# Wrap in DatabaseTokenBackend class
# Accept but ignore team_id parameter
```

### Step 3: Implement Vault Backend
```bash
touch mcpgateway/services/token_backends/vault_backend.py
```

Key methods:
- `_resolve_mcp_url(gateway_id)` - query gateways.url
- `_hash_server_id(mcp_url)` - SHA-256 first 8 chars
- `_construct_vault_path(team_id, mcp_url, email)` - build full path
- `_vault_request(method, path, data)` - HTTP client with retry

### Step 4: Refactor Service to Façade
```python
# token_storage_service.py
class TokenStorageService:
    def __init__(self, db, user_context=None):
        # Select backend based on settings.oauth_token_backend
        # Delegate all 5 methods to backend
```

### Step 5: Create Vault Router
```bash
touch mcpgateway/routers/vault_router.py
```

Endpoints:
- `GET /vault/authorize/{server_id}?gateway_url=...`
- `GET /vault/callback?code=...&state=...`

### Step 6: Update Config
Add 10 new fields to Settings class in config.py

### Step 7: Update Call Sites
Change 5 files:
```python
# OLD
token_service = TokenStorageService(db)

# NEW
token_service = TokenStorageService(db, user_context)
```

### Step 8: Update main.py
```python
if settings.oauth_token_backend == "vault":
    app.include_router(vault_router.router)
```

### Step 9: Add Dependency
```toml
[project.optional-dependencies]
vault = ["hvac>=2.3.0"]
```

---

## 🧪 Testing Order

1. **Database Backend Regression** (Parts A.1-A.4)
   - Verify no behavior changes
   - Existing OAuth flow still works
   - Tokens stored/retrieved from DB

2. **Vault Setup** (Parts B.1-B.3)
   - Vault connectivity
   - Policy permissions
   - Health check

3. **Vault Authorization Flow** (Parts C.1-C.4)
   - `/vault/authorize/{server_id}` works
   - Multi-gateway selection
   - Token stored in Vault (not DB)

4. **Token Retrieval** (Parts D.1-D.3)
   - Get token from Vault
   - Auto-refresh before expiry
   - Expired token handling

5. **Token Revocation** (Part E.1)
   - Delete from Vault

6. **Token Cache** (Parts F.1-F.3)
   - Cache disabled by default
   - Cache hit/miss
   - Cache invalidation

7. **Error Scenarios** (Parts G.1-G.5)
   - Vault unreachable
   - Invalid token
   - Not found errors

8. **Team Scoping** (Parts H.1-H.2)
   - JWT with teams
   - JWT without teams (fallback)

9. **Observability** (Parts I.1-I.2)
   - Metrics
   - Structured logs

10. **Migration** (Parts J.1-J.2)
    - Switch database → vault
    - Switch vault → database

---

## 📊 Vault Secret Payload Example

```json
{
  "email": "alice@example.com",
  "team_id": "engineering",
  "mcp_url": "https://mcp.github.acme.com",
  "token": {
    "access_token": "gho_Aa1Bb2...",
    "refresh_token": "ghr_Rr9Ss8...",
    "scopes": ["repo", "read:user"]
  },
  "user_id": "github_uid_44712",
  "token_type": "Bearer",
  "expires_at": "2026-07-07T18:00:00Z",
  "created_at": "2025-07-07T10:00:00Z",
  "updated_at": "2025-07-07T10:00:00Z"
}
```

**Key differences from database:**
- `mcp_url` replaces `gateway_id` (human-readable, portable)
- `team_id` added (not in DB table yet)
- Tokens plain-text (Vault encrypts at rest)
- No `id` primary key (path is unique identifier)

---

## ⚙️ Configuration Quick Reference

### Database Backend (default)
```bash
OAUTH_TOKEN_BACKEND=database
# No other config needed
```

### Vault Backend (local dev)
```bash
OAUTH_TOKEN_BACKEND=vault
VAULT_ADDR=http://127.0.0.1:8200
VAULT_TOKEN=<your-dev-token>
VAULT_KV_MOUNT=secret
VAULT_KV_PATH_PREFIX=contextforge/oauth
VAULT_TLS_VERIFY=false

# Optional cache
VAULT_TOKEN_CACHE_ENABLED=true
VAULT_TOKEN_CACHE_TTL=300
VAULT_TOKEN_CACHE_MAX_SIZE=1000
```

### Vault Backend (production)
```bash
OAUTH_TOKEN_BACKEND=vault
VAULT_ADDR=https://vault.acme.com:8200
VAULT_TOKEN=<prod-token>
VAULT_KV_MOUNT=secret
VAULT_KV_PATH_PREFIX=contextforge/oauth
VAULT_TLS_VERIFY=true

VAULT_TOKEN_CACHE_ENABLED=true
VAULT_TOKEN_CACHE_TTL=300
VAULT_TOKEN_CACHE_MAX_SIZE=10000
```

---

## 🔍 Debugging Tips

### Check which backend is active
```bash
# Logs at startup
grep "oauth_token_backend" logs/contextforge.log
```

### Verify Vault connectivity
```bash
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=<your-token>
vault status
```

### List tokens in Vault
```bash
vault kv list secret/contextforge/oauth/
# Lists team_ids

vault kv list secret/contextforge/oauth/engineering/
# Lists server_ids under team

vault kv get secret/contextforge/oauth/engineering/647ad7b3/alice%40example.com
# Get full token record
```

### Check database tokens
```bash
sqlite3 mcp.db "SELECT gateway_id, app_user_email, token_type, created_at FROM oauth_tokens;"
```

### Verify no database row when using Vault
```bash
sqlite3 mcp.db "SELECT COUNT(*) FROM oauth_tokens WHERE app_user_email = 'test@example.com';"
# Should be 0 when OAUTH_TOKEN_BACKEND=vault
```

### Check logs for backend operations
```bash
# Vault operations
grep "vault_backend" logs/contextforge.log | jq .

# Database operations
grep "db_backend" logs/contextforge.log | jq .
```

---

## 🚨 Common Pitfalls to Avoid

1. **Don't modify database schema in Phase 1**
   - NO `team_id` column added yet
   - DatabaseTokenBackend accepts but ignores it

2. **Don't expose gateway_id to clients**
   - Clients only know `server_id`
   - Resolution happens server-side

3. **Don't use gateway_id in Vault paths**
   - Use `mcp_url` (from `gateways.url`)
   - Hash to `server_id` for path segment

4. **Don't log sensitive data**
   - Never log `access_token`, `refresh_token`, `VAULT_TOKEN`
   - Use structured logging with `user_email` but not tokens

5. **Don't implement dual-backend fallback**
   - One backend at a time
   - No "try Vault, fallback to DB" logic

6. **Don't skip retry logic in VaultTokenBackend**
   - Always retry 3× with exponential backoff
   - Handle timeouts gracefully

7. **Don't change existing OAuth flow behavior**
   - DatabaseTokenBackend must be EXACT copy
   - Only instantiation changes in call sites

---

## 📚 Documentation References

- Design document: `contextforge-pluggable-token-storage-architect-design-document.html`
- Implementation plan: `IMPLEMENTATION_SUMMARY_VAULT.md`
- Testing plan: `MANUAL_TESTING_VAULT_TOKEN_STORAGE.md`
- Vault setup guide: `docs/vault-local-dev-complete-guide.md` (to be created)

---

## ✅ Pre-Commit Checklist

Before committing:
- [ ] All 9 tasks completed
- [ ] `make ruff` passes
- [ ] `make mypy` passes
- [ ] `make test` passes (existing tests)
- [ ] Manual testing completed (all checkboxes in test plan)
- [ ] No `gateway_id` exposed to clients
- [ ] No database schema changes
- [ ] DatabaseTokenBackend ignores `team_id`
- [ ] VaultTokenBackend uses `mcp_url` in paths
- [ ] No sensitive data in logs
- [ ] Documentation updated

---

**End of Quick Reference**
