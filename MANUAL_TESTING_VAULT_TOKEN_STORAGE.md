# Manual Testing Plan: Vault Token Storage (Feature #5402)

**Status:** Pre-implementation testing plan  
**Date:** 2026-07-09  
**Tester:** _____________  
**Environment:** Local Dev (Vault + PostgreSQL)

---

## Prerequisites

### 1. PostgreSQL Setup
```bash
# Create vault_dev database
psql -U postgres << EOF
CREATE DATABASE vault_dev;
CREATE USER vault_user WITH ENCRYPTED PASSWORD 'vault_dev_password';
GRANT ALL PRIVILEGES ON DATABASE vault_dev TO vault_user;
\c vault_dev
GRANT ALL ON SCHEMA public TO vault_user;
EOF
```

### 2. Vault Server Setup
Follow the complete guide: `docs/vault-local-dev-complete-guide.md`

**Quick verification:**
```bash
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=<your-dev-token>

# Check Vault is running and unsealed
vault status
# Should show: Sealed: false

# Verify KV v2 mount exists
vault secrets list
# Should show: secret/ listed

# Test write/read
vault kv put secret/test-key value="test-value"
vault kv get secret/test-key
vault kv delete secret/test-key
```

### 3. ContextForge Configuration

Create/update `.env`:
```bash
# Test with DATABASE backend first (baseline)
OAUTH_TOKEN_BACKEND=database
DATABASE_URL=sqlite:///./mcp.db

# Then switch to VAULT backend
OAUTH_TOKEN_BACKEND=vault
VAULT_ADDR=http://127.0.0.1:8200
VAULT_TOKEN=<your-dev-token>
VAULT_KV_MOUNT=secret
VAULT_KV_PATH_PREFIX=contextforge/oauth
VAULT_TLS_VERIFY=false

# Optional: Enable token caching for testing
VAULT_TOKEN_CACHE_ENABLED=true
VAULT_TOKEN_CACHE_TTL=300
VAULT_TOKEN_CACHE_MAX_SIZE=1000

# Auth settings
AUTH_REQUIRED=true
JWT_SECRET_KEY=test-secret-key-change-in-production
BASIC_AUTH_USER=admin
BASIC_AUTH_PASSWORD=changeme

# Enable admin API
MCPGATEWAY_ADMIN_API_ENABLED=true
MCPGATEWAY_UI_ENABLED=true
```

### 4. Create Test JWT Token
```bash
python -m mcpgateway.utils.create_jwt_token \
  --username test-user@example.com \
  --exp 10080 \
  --secret test-secret-key-change-in-production

export MCPGATEWAY_BEARER_TOKEN="<generated-token>"
```

### 5. Setup Test Gateway with OAuth

You'll need a real OAuth provider (GitHub, Google, etc.) or use mock values for testing.

**Example: GitHub OAuth App**
1. Go to GitHub Settings → Developer settings → OAuth Apps
2. Create new OAuth App:
   - Application name: `ContextForge Dev`
   - Homepage URL: `http://localhost:4444`
   - Authorization callback URL: `http://localhost:4444/oauth/callback` (for DB backend) and `http://localhost:4444/vault/callback` (for Vault backend)
3. Note the Client ID and generate a Client Secret

**Register gateway in ContextForge:**
```bash
# POST /gateways
curl -X POST http://localhost:4444/gateways \
  -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "GitHub MCP Test",
    "url": "https://mcp.github.example.com",
    "auth_type": "oauth",
    "oauth_config": {
      "client_id": "<your-github-client-id>",
      "client_secret": "<your-github-client-secret>",
      "authorization_url": "https://github.com/login/oauth/authorize",
      "token_url": "https://github.com/login/oauth/access_token",
      "scopes": ["repo", "read:user"]
    }
  }'

# Note the returned gateway_id
export GATEWAY_ID="<returned-gateway-id>"
```

**Create virtual server:**
```bash
# POST /servers
curl -X POST http://localhost:4444/servers \
  -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Virtual Server",
    "description": "For Vault token storage testing"
  }'

# Note the returned server_id
export SERVER_ID="<returned-server-id>"
```

**Link gateway to server via a tool:**
```bash
# POST /tools
curl -X POST http://localhost:4444/tools \
  -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test-tool",
    "gateway_id": "'"$GATEWAY_ID"'",
    "description": "Test tool for OAuth flow"
  }'

export TOOL_ID="<returned-tool-id>"

# POST /servers/{server_id}/tools/{tool_id}
curl -X POST http://localhost:4444/servers/$SERVER_ID/tools/$TOOL_ID \
  -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN"
```

---

## Test Suite

### Part A: Database Backend Regression Testing (Baseline)

**Objective:** Verify no behavior changes to existing database backend

#### A.1 ✅ Database Backend Still Works
```bash
# Ensure .env has:
OAUTH_TOKEN_BACKEND=database

# Start server
make dev

# Check startup logs
# Expected: No "vault" related logs
# Expected: Server starts successfully
```

**Pass criteria:** Server starts without errors, no Vault-related logs

#### A.2 ✅ Existing OAuth Flow Unchanged
```bash
# Visit authorization URL (existing endpoint)
# NOTE: Use /oauth/authorize for database backend
curl -i -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  "http://localhost:4444/oauth/authorize?gateway_id=$GATEWAY_ID"

# Expected: 302 redirect to GitHub OAuth
# Expected: Redirects to GitHub authorization page
```

**Pass criteria:** Redirect to IdP, callback succeeds, token stored in `oauth_tokens` table

#### A.3 ✅ Token Storage in Database
```bash
# After completing OAuth flow in browser, check database
sqlite3 mcp.db << EOF
SELECT gateway_id, app_user_email, token_type, 
       substr(access_token, 1, 20) as token_preview,
       expires_at, created_at
FROM oauth_tokens;
EOF
```

**Pass criteria:** 
- Row exists with correct gateway_id and email
- access_token is encrypted (not plain text)
- expires_at populated if IdP provided it

#### A.4 ✅ Token Retrieval Works
```bash
# Make a tool call that requires OAuth token
# The backend should auto-retrieve and decrypt token

# Check logs for token retrieval
# Expected: "Retrieved OAuth token for user=test-user@example.com gateway_id=..."
```

**Pass criteria:** Token retrieved, decrypted, used for upstream call

---

### Part B: Vault Backend Setup Verification

**Objective:** Verify Vault connectivity and configuration

#### B.1 ✅ Switch to Vault Backend
```bash
# Update .env:
OAUTH_TOKEN_BACKEND=vault
VAULT_ADDR=http://127.0.0.1:8200
VAULT_TOKEN=<your-dev-token>

# Restart server
make dev
```

**Pass criteria:** 
- Startup log shows: `oauth_token_backend=vault vault_addr=http://127.0.0.1:8200 ✓ reachable`
- No errors about missing Vault config

#### B.2 ✅ Vault Health Check
```bash
# Check /health endpoint includes Vault status
curl http://localhost:4444/health | jq .

# Expected output should include:
# {
#   "status": "healthy",
#   "vault": {
#     "backend": "vault",
#     "addr": "http://127.0.0.1:8200",
#     "reachable": true
#   }
# }
```

**Pass criteria:** Vault reachable = true

#### B.3 ✅ Vault Policy Permissions
```bash
# Test write permission
vault kv put secret/contextforge/oauth/test-team/test-server/test@example.com \
  email="test@example.com" \
  team_id="test-team" \
  mcp_url="https://test.example.com"

# Test read permission
vault kv get secret/contextforge/oauth/test-team/test-server/test@example.com

# Test delete permission
vault kv delete secret/contextforge/oauth/test-team/test-server/test@example.com

# Test metadata list permission
vault kv list secret/contextforge/oauth/
```

**Pass criteria:** All operations succeed, no permission denied errors

---

### Part C: Vault Authorization Flow

**Objective:** Test new `/vault/authorize` and `/vault/callback` endpoints

#### C.1 ✅ New Vault Authorize Endpoint Exists
```bash
# GET /vault/authorize/{server_id}
curl -i -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  "http://localhost:4444/vault/authorize/$SERVER_ID"

# Expected: 302 redirect to IdP (GitHub)
# Expected: Redirect URL contains state parameter and PKCE challenge
```

**Pass criteria:** 
- Status 302
- Location header contains IdP authorization URL
- State saved in `oauth_states` table

#### C.2 ✅ Multi-Gateway Server Selection
**Setup:** Create a second gateway and link to same server
```bash
# Create second gateway (e.g., Google OAuth or Jira)
curl -X POST http://localhost:4444/gateways \
  -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Second OAuth Gateway",
    "url": "https://mcp.example2.com",
    "auth_type": "oauth",
    "oauth_config": { ... }
  }'

export GATEWAY_ID_2="<returned-gateway-id>"

# Link second gateway to same server via tool
# ... (similar to setup steps)

# Test without gateway_url parameter (should show selection or pick first)
curl -i -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  "http://localhost:4444/vault/authorize/$SERVER_ID"

# Test with explicit gateway_url parameter
curl -i -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  "http://localhost:4444/vault/authorize/$SERVER_ID?gateway_url=https://mcp.github.example.com"
```

**Pass criteria:**
- Without param: 302 to first OAuth gateway OR selection page
- With param: 302 to specified gateway's IdP

#### C.3 ✅ Vault Callback Stores to Vault
**Manual step:** Complete OAuth flow in browser
1. Visit authorize URL from C.1
2. Approve on IdP (GitHub)
3. Browser redirected to `/vault/callback?code=...&state=...`

**Verification:**
```bash
# Check Vault directly - token should be stored
# Derive server_id from gateway URL
export MCP_URL="https://mcp.github.example.com"
export SERVER_ID_HASH=$(echo -n "$MCP_URL" | sha256sum | cut -c1-8)

vault kv get secret/contextforge/oauth/default/$SERVER_ID_HASH/test-user%40example.com

# Expected output:
# ====== Data ======
# Key             Value
# ---             -----
# email           test-user@example.com
# team_id         default
# mcp_url         https://mcp.github.example.com
# token           map[access_token:gho_... refresh_token:ghr_... scopes:[repo read:user]]
# user_id         <github-user-id>
# token_type      Bearer
# expires_at      2026-07-07T18:00:00Z
# created_at      2025-07-07T10:00:00Z
# updated_at      2025-07-07T10:00:00Z
```

**Pass criteria:**
- Secret exists in Vault at correct path
- Path includes team_id (default), server_id (hash), url-encoded email
- Payload contains mcp_url (not gateway_id)
- Tokens are plain-text in Vault (Vault encrypts at rest)
- NO row in `oauth_tokens` database table

#### C.4 ✅ Verify No Database Row Created
```bash
sqlite3 mcp.db << EOF
SELECT COUNT(*) FROM oauth_tokens WHERE app_user_email = 'test-user@example.com';
EOF

# Expected: 0 (when using Vault backend)
```

**Pass criteria:** Count = 0

---

### Part D: Token Retrieval and Usage

**Objective:** Verify VaultTokenBackend.get_user_token() works

#### D.1 ✅ Token Retrieved from Vault
```bash
# Make a tool call that requires OAuth
# Server should retrieve token from Vault

# Check logs for:
# INFO  component=vault_backend operation=get_user_token gateway_id=... user_email=test-user@example.com duration_ms=...
```

**Pass criteria:** 
- Tool call succeeds
- Log shows Vault backend retrieval
- Duration is reasonable (<100ms for local Vault)

#### D.2 ✅ Token Auto-Refresh Before Expiry
**Setup:** Create a token that expires soon (mock or wait)

```bash
# Manually update token in Vault with near-expiry
vault kv patch secret/contextforge/oauth/default/$SERVER_ID_HASH/test-user%40example.com \
  expires_at="$(date -u -d '+4 minutes' +%Y-%m-%dT%H:%M:%SZ)"

# Make tool call - should trigger refresh (threshold_seconds=300)
# Check logs for refresh operation
```

**Pass criteria:**
- Token refreshed automatically
- New token stored in Vault
- Tool call succeeds with refreshed token

#### D.3 ✅ Expired Token Handling
```bash
# Manually update token with past expiry
vault kv patch secret/contextforge/oauth/default/$SERVER_ID_HASH/test-user%40example.com \
  expires_at="$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)"

# Make tool call
# Expected: 401 error or redirect to re-authorize
```

**Pass criteria:**
- Error response with actionable message
- User directed to `/vault/authorize/{server_id}` to re-authorize

---

### Part E: Token Revocation

**Objective:** Verify VaultTokenBackend.revoke_user_tokens() works

#### E.1 ✅ Revoke Token via API
```bash
# DELETE /gateways/{gateway_id}/tokens
curl -X DELETE http://localhost:4444/gateways/$GATEWAY_ID/tokens \
  -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  -d '{"app_user_email": "test-user@example.com"}'

# Check Vault - secret should be deleted
vault kv get secret/contextforge/oauth/default/$SERVER_ID_HASH/test-user%40example.com
# Expected: Error: no data found
```

**Pass criteria:**
- API returns success
- Vault secret deleted
- Next tool call requires re-authorization

---

### Part F: Token Caching (Optional Feature)

**Objective:** Verify in-memory token cache works when enabled

#### F.1 ✅ Cache Disabled by Default
```bash
# Ensure .env has:
VAULT_TOKEN_CACHE_ENABLED=false

# Make multiple tool calls
# Check logs - each should hit Vault
```

**Pass criteria:** Every `get_user_token` call logs Vault GET operation

#### F.2 ✅ Cache Enabled - Cache Hit
```bash
# Update .env:
VAULT_TOKEN_CACHE_ENABLED=true
VAULT_TOKEN_CACHE_TTL=300

# Restart server
make dev

# Make first tool call - should MISS cache, fetch from Vault
# Make second tool call within 5min - should HIT cache

# Check metrics (if exposed):
curl http://localhost:4444/metrics | grep vault_token_cache
# vault_token_cache_hit_total
# vault_token_cache_miss_total
```

**Pass criteria:**
- First call: cache miss, Vault GET logged
- Second call: cache hit, no Vault GET, faster response

#### F.3 ✅ Cache Invalidation on Write
```bash
# With cache enabled, make a tool call (populate cache)
# Trigger token refresh (manually update expiry to near-expiry)
# Make another tool call - should refresh and invalidate cache
# Next call should fetch fresh token from Vault or cache
```

**Pass criteria:** 
- Refresh invalidates cache entry
- Next read fetches updated token

---

### Part G: Error Scenarios

**Objective:** Verify error handling and resilience

#### G.1 ✅ Vault Unreachable
```bash
# Stop Vault server
killall vault

# Make tool call requiring OAuth
# Expected: 503 Service Unavailable with user-friendly error
```

**Expected response:**
```json
{
  "error": {
    "code": "OAUTH_TOKEN_UNAVAILABLE",
    "message": "Could not retrieve your OAuth credentials. Please re-authorize.",
    "details": "The credential storage system is temporarily unavailable.",
    "action": {
      "text": "Click here to re-authorize",
      "url": "/vault/authorize/647ad7b348044bce8fa27a2157b00a0d"
    }
  }
}
```

**Pass criteria:**
- User-friendly error message
- No internal Vault URLs or paths exposed
- Retry logic attempted (3 attempts logged)

#### G.2 ✅ Invalid VAULT_TOKEN
```bash
# Update .env with invalid token:
VAULT_TOKEN=invalid-token-12345

# Restart server
make dev

# Expected: Critical log at startup or first operation
# "Vault auth failure — VAULT_TOKEN invalid or expired"
```

**Pass criteria:**
- Clear error message
- Server handles gracefully (doesn't crash)

#### G.3 ✅ Token Not Found (First Time User)
```bash
# New user makes tool call without authorizing first
# Expected: Redirect or error with authorization URL
```

**Pass criteria:**
- 401 or 403 with clear message
- Response includes `/vault/authorize/{server_id}` link

#### G.4 ✅ Invalid server_id
```bash
curl -i -H "Authorization: Bearer $MCPGATEWAY_BEARER_TOKEN" \
  "http://localhost:4444/vault/authorize/invalid-uuid-12345"

# Expected: 404 Not Found
```

**Pass criteria:** 404 with message "Server not found"

#### G.5 ✅ Server Has No OAuth Gateways
```bash
# Create server with no OAuth-enabled gateways
# Try to authorize
# Expected: 400 Bad Request "No OAuth gateways configured for this server"
```

**Pass criteria:** Clear error message explaining issue

---

### Part H: Team-Scoped Token Storage

**Objective:** Verify team_id extraction and Vault path construction

#### H.1 ✅ JWT with Teams Claim
```bash
# Create JWT with teams claim
python -m mcpgateway.utils.create_jwt_token \
  --username test-user@example.com \
  --exp 10080 \
  --secret test-secret-key-change-in-production \
  --extra-claims '{"teams": ["engineering", "sales"]}'

export MCPGATEWAY_BEARER_TOKEN="<generated-token-with-teams>"

# Complete OAuth flow
# Check Vault path uses first team
vault kv list secret/contextforge/oauth/
# Should show: engineering/

vault kv get secret/contextforge/oauth/engineering/$SERVER_ID_HASH/test-user%40example.com
# Should exist with team_id=engineering
```

**Pass criteria:**
- Token stored under team_id="engineering" (first in list)
- Payload contains team_id field

#### H.2 ✅ JWT without Teams Claim (Fallback)
```bash
# Use JWT without teams claim (from earlier tests)
# Complete OAuth flow
# Check Vault path uses default

vault kv get secret/contextforge/oauth/default/$SERVER_ID_HASH/test-user%40example.com
# Should exist with team_id=default
```

**Pass criteria:** Fallback to "default" team_id works

---

### Part I: Observability

**Objective:** Verify metrics, logs, and monitoring

#### I.1 ✅ Metrics Exposed
```bash
# If Prometheus metrics enabled
curl http://localhost:4444/metrics | grep contextforge_token_backend

# Expected metrics:
# contextforge_token_backend_operations_total{backend="vault",operation="store",status="success"}
# contextforge_token_backend_operations_total{backend="vault",operation="get",status="success"}
# contextforge_token_backend_latency_seconds{backend="vault",operation="get"}
# contextforge_vault_errors_total{error_type="timeout"}
# contextforge_vault_healthy{vault_addr="http://127.0.0.1:8200"}
```

**Pass criteria:** Metrics present and incrementing

#### I.2 ✅ Structured Logging
```bash
# Check logs for structured output
# Expected JSON with fields:
# - component: "vault_backend"
# - operation: "store_tokens" | "get_user_token"
# - gateway_id, mcp_url, user_email
# - duration_ms
# - NO access_token or refresh_token values
```

**Pass criteria:** 
- Logs structured and parseable
- No sensitive data (tokens, secrets) logged

---

## Part J: Migration and Coexistence

**Objective:** Verify backend switching behavior

#### J.1 ✅ Switch from Database to Vault (Existing Tokens)
```bash
# Start with OAUTH_TOKEN_BACKEND=database
# Complete OAuth flow - token in DB

# Switch to OAUTH_TOKEN_BACKEND=vault
# Restart server

# Make tool call
# Expected: 404 token not found, redirect to re-authorize

# Complete new OAuth flow
# Verify token now in Vault
```

**Pass criteria:**
- Old DB tokens ignored when using Vault backend
- User must re-authorize (expected behavior Phase 1)

#### J.2 ✅ Switch from Vault to Database (Fallback Scenario)
```bash
# Start with OAUTH_TOKEN_BACKEND=vault
# Complete OAuth flow - token in Vault

# Switch to OAUTH_TOKEN_BACKEND=database
# Restart server

# Make tool call
# Expected: 404 token not found, redirect to /oauth/authorize

# Complete OAuth flow
# Verify token in database
```

**Pass criteria:**
- Vault tokens ignored when using database backend
- Clean separation, no cross-contamination

---

## Test Checklist Summary

| Test ID | Test Name | Pass | Fail | Notes |
|---------|-----------|------|------|-------|
| A.1 | Database backend still works | ☐ | ☐ | |
| A.2 | Existing OAuth flow unchanged | ☐ | ☐ | |
| A.3 | Token storage in database | ☐ | ☐ | |
| A.4 | Token retrieval works | ☐ | ☐ | |
| B.1 | Switch to Vault backend | ☐ | ☐ | |
| B.2 | Vault health check | ☐ | ☐ | |
| B.3 | Vault policy permissions | ☐ | ☐ | |
| C.1 | New vault authorize endpoint | ☐ | ☐ | |
| C.2 | Multi-gateway server selection | ☐ | ☐ | |
| C.3 | Vault callback stores to Vault | ☐ | ☐ | |
| C.4 | No database row created | ☐ | ☐ | |
| D.1 | Token retrieved from Vault | ☐ | ☐ | |
| D.2 | Token auto-refresh | ☐ | ☐ | |
| D.3 | Expired token handling | ☐ | ☐ | |
| E.1 | Revoke token via API | ☐ | ☐ | |
| F.1 | Cache disabled by default | ☐ | ☐ | |
| F.2 | Cache enabled - cache hit | ☐ | ☐ | |
| F.3 | Cache invalidation on write | ☐ | ☐ | |
| G.1 | Vault unreachable | ☐ | ☐ | |
| G.2 | Invalid VAULT_TOKEN | ☐ | ☐ | |
| G.3 | Token not found (first time) | ☐ | ☐ | |
| G.4 | Invalid server_id | ☐ | ☐ | |
| G.5 | Server has no OAuth gateways | ☐ | ☐ | |
| H.1 | JWT with teams claim | ☐ | ☐ | |
| H.2 | JWT without teams claim | ☐ | ☐ | |
| I.1 | Metrics exposed | ☐ | ☐ | |
| I.2 | Structured logging | ☐ | ☐ | |
| J.1 | Switch database → vault | ☐ | ☐ | |
| J.2 | Switch vault → database | ☐ | ☐ | |

---

## Issues Found During Testing

| Issue # | Description | Severity | Status |
|---------|-------------|----------|--------|
| | | | |
| | | | |

---

## Sign-off

**Tested by:** _____________  
**Date:** _____________  
**Result:** ☐ PASS ☐ FAIL ☐ CONDITIONAL PASS

**Notes:**

