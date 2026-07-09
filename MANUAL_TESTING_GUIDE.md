# Manual Testing Guide: Vault & Database Token Storage

**Feature #5402 | Phase 1**  
**Purpose:** Verify pluggable OAuth token storage works correctly for both Database and Vault backends  
**Date:** 2026-07-09

---

## 🎯 Testing Overview

This guide walks you through testing both token storage backends:
1. **Database Backend** (existing behavior - baseline)
2. **Vault Backend** (new feature)

**Testing Time:** ~2-3 hours  
**Prerequisites:** Local dev environment, PostgreSQL, Vault server

---

## 📋 Quick Checklist

Use this to track your progress:

- [ ] **Section 1:** Environment Setup (30 min)
- [ ] **Section 2:** Database Backend Testing (30 min)
- [ ] **Section 3:** Vault Backend Testing (60 min)
- [ ] **Section 4:** Backend Switching (15 min)
- [ ] **Section 5:** Cleanup

---

## SECTION 1: Environment Setup

### Step 1.1: Install Dependencies

```bash
# Install with Vault support
pip install -e ".[vault]"

# Verify installation
python -c "import hvac; print('hvac installed:', hvac.__version__)"
```

**Expected:** Should print `hvac installed: 2.x.x`

### Step 1.2: Setup PostgreSQL for Vault

```bash
# Connect to PostgreSQL
psql -U postgres

# Create vault database
CREATE DATABASE vault_dev;
CREATE USER vault_user WITH ENCRYPTED PASSWORD 'vault_dev_password';
GRANT ALL PRIVILEGES ON DATABASE vault_dev TO vault_user;

# Connect to vault_dev and grant schema permissions
\c vault_dev
GRANT ALL ON SCHEMA public TO vault_user;

# Create the Vault storage table manually
CREATE TABLE vault_kv_store (
    parent_path TEXT COLLATE "C" NOT NULL,
    path        TEXT COLLATE "C",
    key         TEXT COLLATE "C",
    value       BYTEA,
    CONSTRAINT pkey PRIMARY KEY (path, key)
);

CREATE INDEX parent_path_idx ON vault_kv_store (parent_path);

\q
```

**Expected:** Tables created without errors

### Step 1.3: Setup HashiCorp Vault

```bash
# Create Vault config directory
mkdir -p ~/.vault-config

# Create Vault configuration file
cat > ~/.vault-config/config.hcl << 'EOF'
storage "postgresql" {
  connection_url = "postgres://vault_user:vault_dev_password@localhost:5432/vault_dev?sslmode=disable"
  table          = "vault_kv_store"
  max_parallel   = "128"
}

listener "tcp" {
  address     = "127.0.0.1:8200"
  tls_disable = 1
}

api_addr = "http://127.0.0.1:8200"
ui       = true
log_level = "info"
EOF

# Start Vault server
vault server -config=~/.vault-config/config.hcl &

# Wait a few seconds for Vault to start
sleep 5

# Initialize Vault (save the output!)
export VAULT_ADDR='http://127.0.0.1:8200'
vault operator init -key-shares=1 -key-threshold=1 > ~/.vault-config/vault-init.txt

# Extract unseal key and root token
export VAULT_UNSEAL_KEY=$(grep 'Unseal Key 1:' ~/.vault-config/vault-init.txt | awk '{print $4}')
export VAULT_ROOT_TOKEN=$(grep 'Initial Root Token:' ~/.vault-config/vault-init.txt | awk '{print $4}')

# Unseal Vault
vault operator unseal $VAULT_UNSEAL_KEY

# Login with root token
vault login $VAULT_ROOT_TOKEN

# Enable KV v2 secrets engine
vault secrets enable -version=2 -path=secret kv

# Create policy for ContextForge
cat > /tmp/contextforge-policy.hcl << 'EOF'
path "secret/data/contextforge/oauth/*" {
  capabilities = ["create", "update", "read", "delete"]
}
path "secret/metadata/contextforge/oauth/*" {
  capabilities = ["delete", "list"]
}
EOF

vault policy write contextforge /tmp/contextforge-policy.hcl

# Create token for ContextForge
export VAULT_TOKEN=$(vault token create -policy="contextforge" -ttl="24h" -format=json | jq -r '.auth.client_token')

echo "✅ Vault setup complete!"
echo "VAULT_TOKEN=$VAULT_TOKEN"
echo ""
echo "Save this token - you'll need it for .env configuration"
```

**Expected:**
- Vault starts without errors
- Initialization creates unseal key and root token
- Vault unseals successfully
- KV v2 engine enabled
- Policy created
- Token generated

**⚠️ Important:** Save the `VAULT_TOKEN` value - you'll use it in `.env`

### Step 1.4: Setup GitHub OAuth App (Test Provider)

1. Go to GitHub Settings → Developer settings → OAuth Apps
2. Click "New OAuth App"
3. Fill in:
   - **Application name:** `ContextForge Dev Test`
   - **Homepage URL:** `http://localhost:4444`
   - **Authorization callback URL:** `http://localhost:4444/oauth/callback`
4. Click "Register application"
5. **Note the Client ID**
6. Click "Generate a new client secret"
7. **Copy the Client Secret** (you'll only see it once)

**Expected:** You have Client ID and Client Secret for testing

### Step 1.5: Create Test Gateway and Server

```bash
# Start ContextForge (database backend first)
cp .env.example .env

# Configure .env for DATABASE backend
cat >> .env << EOF

# OAuth Token Storage Backend
OAUTH_TOKEN_BACKEND=database

# JWT for testing
JWT_SECRET_KEY=test-secret-key-for-manual-testing
AUTH_ENCRYPTION_SECRET=test-encryption-secret-for-manual-testing

# Enable admin API
MCPGATEWAY_ADMIN_API_ENABLED=true
MCPGATEWAY_UI_ENABLED=true
EOF

# Start server
make dev &

# Wait for server to start
sleep 5

# Create JWT token
export CF_TOKEN=$(python -m mcpgateway.utils.create_jwt_token \
  --username test-user@example.com \
  --exp 10080 \
  --secret test-secret-key-for-manual-testing)

echo "✅ ContextForge Bearer Token: $CF_TOKEN"

# Create OAuth gateway
GATEWAY_RESPONSE=$(curl -s -X POST http://localhost:4444/gateways \
  -H "Authorization: Bearer $CF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "GitHub MCP Test",
    "url": "https://mcp.github.example.com",
    "auth_type": "oauth",
    "oauth_config": {
      "client_id": "YOUR_GITHUB_CLIENT_ID",
      "client_secret": "YOUR_GITHUB_CLIENT_SECRET",
      "authorization_url": "https://github.com/login/oauth/authorize",
      "token_url": "https://github.com/login/oauth/access_token",
      "scopes": ["repo", "read:user"]
    }
  }')

export GATEWAY_ID=$(echo $GATEWAY_RESPONSE | jq -r '.id')
echo "✅ Gateway ID: $GATEWAY_ID"

# Create virtual server
SERVER_RESPONSE=$(curl -s -X POST http://localhost:4444/servers \
  -H "Authorization: Bearer $CF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Virtual Server",
    "description": "For Vault token storage testing"
  }')

export SERVER_ID=$(echo $SERVER_RESPONSE | jq -r '.id')
echo "✅ Server ID: $SERVER_ID"

# Create a tool to link gateway and server
TOOL_RESPONSE=$(curl -s -X POST http://localhost:4444/tools \
  -H "Authorization: Bearer $CF_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"test-tool\",
    \"gateway_id\": \"$GATEWAY_ID\",
    \"description\": \"Test tool for OAuth flow\"
  }")

export TOOL_ID=$(echo $TOOL_RESPONSE | jq -r '.id')
echo "✅ Tool ID: $TOOL_ID"

# Link tool to server
curl -s -X POST "http://localhost:4444/servers/$SERVER_ID/tools/$TOOL_ID" \
  -H "Authorization: Bearer $CF_TOKEN"

echo ""
echo "✅ Setup complete! IDs saved in environment variables:"
echo "   GATEWAY_ID=$GATEWAY_ID"
echo "   SERVER_ID=$SERVER_ID"
echo "   TOOL_ID=$TOOL_ID"
echo "   CF_TOKEN=$CF_TOKEN"
```

**Expected:** Gateway, Server, and Tool created successfully

---

## SECTION 2: Database Backend Testing (Baseline)

### Test 2.1: Verify Database Backend is Active

```bash
# Check startup logs
curl -s http://localhost:4444/health | jq .

# Look in logs for backend confirmation
grep -i "token storage backend" logs/contextforge.log
```

**Expected:**
- Health endpoint returns healthy status
- No "vault" mentioned in logs (database backend is default)

### Test 2.2: Initiate OAuth Flow (Database)

```bash
# Open in browser or use curl
OAUTH_URL="http://localhost:4444/oauth/authorize?gateway_id=$GATEWAY_ID"
echo "Visit this URL to authorize: $OAUTH_URL"

# Or use curl to check redirect
curl -i -H "Authorization: Bearer $CF_TOKEN" "$OAUTH_URL"
```

**Expected:**
- Returns 302 redirect to GitHub OAuth
- State parameter is present in redirect URL

### Test 2.3: Complete OAuth Flow

1. **Visit the OAuth URL** from Test 2.2 in your browser
2. **Click "Authorize"** on GitHub
3. **Redirected to callback:** `http://localhost:4444/oauth/callback?code=...&state=...`
4. **See success page:** "OAuth Authorization Successful"

**Expected:** Success page displayed, no errors

### Test 2.4: Verify Token Stored in Database

```bash
# Check database
sqlite3 mcp.db << EOF
SELECT 
  gateway_id, 
  app_user_email, 
  token_type,
  length(access_token) as token_length,
  expires_at,
  created_at
FROM oauth_tokens;
EOF
```

**Expected:**
- One row exists
- `gateway_id` matches your `$GATEWAY_ID`
- `app_user_email` is `test-user@example.com`
- `token_type` is `Bearer`
- `access_token` is encrypted (long string ~200+ chars)
- `expires_at` and `created_at` populated

### Test 2.5: Verify Token Retrieval

```bash
# Check logs while making a "tool call" (simulate)
# The gateway service should retrieve the token

# View token info via API
curl -s -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/gateways/$GATEWAY_ID/tokens?app_user_email=test-user@example.com" | jq .
```

**Expected:**
- Token info returned (scopes, expiry, status)
- No actual token values exposed (security check)

### Test 2.6: Revoke Token

```bash
# Revoke the token
curl -s -X DELETE \
  -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/gateways/$GATEWAY_ID/tokens?app_user_email=test-user@example.com"

# Verify deleted
sqlite3 mcp.db "SELECT COUNT(*) FROM oauth_tokens WHERE app_user_email='test-user@example.com';"
```

**Expected:**
- DELETE returns success
- COUNT returns 0 (token deleted)

**✅ Database Backend Baseline: PASS**

---

## SECTION 3: Vault Backend Testing

### Test 3.1: Switch to Vault Backend

```bash
# Stop ContextForge
pkill -f "uvicorn mcpgateway.main:app"

# Update .env for VAULT backend
cat > .env << EOF
# OAuth Token Storage Backend
OAUTH_TOKEN_BACKEND=vault

# Vault Configuration
VAULT_ADDR=http://127.0.0.1:8200
VAULT_TOKEN=$VAULT_TOKEN
VAULT_KV_MOUNT=secret
VAULT_KV_PATH_PREFIX=contextforge/oauth
VAULT_TLS_VERIFY=false

# Enable cache for testing
VAULT_TOKEN_CACHE_ENABLED=true
VAULT_TOKEN_CACHE_TTL=300
VAULT_TOKEN_CACHE_MAX_SIZE=1000

# JWT for testing
JWT_SECRET_KEY=test-secret-key-for-manual-testing
AUTH_ENCRYPTION_SECRET=test-encryption-secret-for-manual-testing

# Database
DATABASE_URL=sqlite:///./mcp.db

# Enable admin API
MCPGATEWAY_ADMIN_API_ENABLED=true
MCPGATEWAY_UI_ENABLED=true
EOF

# Start server
make dev &

# Wait for startup
sleep 5
```

**Expected:** Server starts successfully

### Test 3.2: Verify Vault Backend is Active

```bash
# Check logs for Vault backend confirmation
grep -i "vault" logs/contextforge.log | tail -5

# Should see:
# "Token storage backend: Vault (addr=http://127.0.0.1:8200)"
# "Vault OAuth router included"
```

**Expected:**
- Log confirms Vault backend is active
- Log shows vault_addr
- Vault OAuth router registered

### Test 3.3: Test New /vault/authorize Endpoint

```bash
# Use the NEW vault endpoint with server_id (not gateway_id)
VAULT_OAUTH_URL="http://localhost:4444/vault/authorize/$SERVER_ID"
echo "Visit this URL to authorize via Vault: $VAULT_OAUTH_URL"

# Check redirect
curl -i -H "Authorization: Bearer $CF_TOKEN" "$VAULT_OAUTH_URL"
```

**Expected:**
- Returns 302 redirect to GitHub OAuth
- Uses `/vault/callback` as callback URL (not `/oauth/callback`)

### Test 3.4: Complete Vault OAuth Flow

1. **Visit the Vault OAuth URL** from Test 3.3 in your browser
2. **Click "Authorize"** on GitHub (may auto-approve if already authorized)
3. **Redirected to:** `http://localhost:4444/vault/callback?code=...&state=...`
4. **See success page:** "OAuth Authorization Successful" + "stored in Vault"

**Expected:** Success page specifically mentions Vault storage

### Test 3.5: Verify Token Stored in Vault (NOT Database)

```bash
# Check database - should be EMPTY
sqlite3 mcp.db "SELECT COUNT(*) FROM oauth_tokens WHERE app_user_email='test-user@example.com';"

# Expected: 0 (no database row)

# Check Vault storage
# First, derive server_id from gateway URL
export MCP_URL="https://mcp.github.example.com"
export SERVER_ID_HASH=$(echo -n "$MCP_URL" | sha256sum | cut -c1-8)

echo "Server ID hash: $SERVER_ID_HASH"

# List Vault secrets
vault kv list secret/contextforge/oauth/

# Should see: default/ (or other team_id if JWT has teams)

# Read the actual token from Vault
vault kv get "secret/contextforge/oauth/default/$SERVER_ID_HASH/test-user%40example.com"
```

**Expected:**
- Database COUNT = 0 (no database row)
- Vault list shows team_id directory (e.g., `default/`)
- Vault get returns token data with:
  - `email`: test-user@example.com
  - `team_id`: default
  - `mcp_url`: https://mcp.github.example.com
  - `token.access_token`: gho_... (plain-text)
  - `token.refresh_token`: ghr_... (if provided)
  - `token.scopes`: ["repo", "read:user"]
  - `expires_at`, `created_at`, `updated_at`

### Test 3.6: Verify Vault Path Structure

```bash
# Check PostgreSQL to see encrypted Vault data
psql -U vault_user -d vault_dev -c "
  SELECT 
    parent_path, 
    path, 
    length(value) as encrypted_size_bytes
  FROM vault_kv_store 
  WHERE path LIKE '%contextforge%'
  ORDER BY path;
"
```

**Expected:**
- Rows show Vault path structure
- `value` is BYTEA (encrypted binary data)
- Path includes team_id and server_id

### Test 3.7: Test Token Cache

```bash
# Make first "token retrieval" (cache miss)
time curl -s -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/gateways/$GATEWAY_ID/tokens?app_user_email=test-user@example.com" | jq .

# Make second retrieval (cache hit - should be faster)
time curl -s -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/gateways/$GATEWAY_ID/tokens?app_user_email=test-user@example.com" | jq .
```

**Expected:**
- First request: ~25-50ms (Vault API call)
- Second request: ~1-5ms (cache hit)
- Both return same token info

### Test 3.8: Test Multi-Gateway Server (Optional)

If you have a second OAuth gateway, test the `?gateway_url=` parameter:

```bash
# Create a second OAuth gateway (e.g., Google)
# ... (similar to Step 1.5) ...

# Authorize with explicit gateway_url selection
curl -i -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/vault/authorize/$SERVER_ID?gateway_url=https://mcp.github.example.com"
```

**Expected:** Redirects to the specific gateway's OAuth provider

### Test 3.9: Test Token Revocation in Vault

```bash
# Revoke token via API
curl -s -X DELETE \
  -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/gateways/$GATEWAY_ID/tokens?app_user_email=test-user@example.com"

# Verify deleted from Vault
vault kv get "secret/contextforge/oauth/default/$SERVER_ID_HASH/test-user%40example.com"

# Expected: Error: no data found (404)
```

**Expected:**
- DELETE returns success
- Vault no longer has the secret
- Database still empty (COUNT = 0)

### Test 3.10: Test Error Scenarios

#### A. Invalid server_id

```bash
curl -i -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/vault/authorize/invalid-uuid-12345"

# Expected: 404 Not Found
```

#### B. Server with no OAuth gateways

```bash
# Create server without OAuth tools
NO_OAUTH_SERVER=$(curl -s -X POST http://localhost:4444/servers \
  -H "Authorization: Bearer $CF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"No OAuth Server"}' | jq -r '.id')

curl -i -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/vault/authorize/$NO_OAUTH_SERVER"

# Expected: 400 Bad Request "No OAuth gateways configured"
```

#### C. Vault unreachable

```bash
# Stop Vault temporarily
pkill vault

# Try to authorize
curl -i -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/vault/authorize/$SERVER_ID"

# Expected: 500 Internal Server Error
# Check logs for: "Vault unreachable after 3 attempts"

# Restart Vault
vault server -config=~/.vault-config/config.hcl &
sleep 3
vault operator unseal $VAULT_UNSEAL_KEY
```

**✅ Vault Backend Testing: PASS**

---

## SECTION 4: Backend Switching

### Test 4.1: Switch from Vault to Database

```bash
# Authorize a token in Vault first (if not already done)
# ... (re-run Test 3.4 if needed) ...

# Stop server
pkill -f "uvicorn mcpgateway.main:app"

# Switch to database backend
sed -i 's/OAUTH_TOKEN_BACKEND=vault/OAUTH_TOKEN_BACKEND=database/' .env

# Start server
make dev &
sleep 5

# Check logs
grep "Token storage backend" logs/contextforge.log | tail -1
# Expected: "Token storage backend: Database"

# Try to retrieve token
curl -s -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/gateways/$GATEWAY_ID/tokens?app_user_email=test-user@example.com"

# Expected: 404 or "no tokens found" (Vault tokens not visible in database mode)

# Check database
sqlite3 mcp.db "SELECT COUNT(*) FROM oauth_tokens WHERE app_user_email='test-user@example.com';"
# Expected: 0 (Vault tokens ignored)
```

**Expected:**
- Backend switches to database
- Vault tokens are NOT visible
- No errors or crashes

### Test 4.2: Switch from Database to Vault

```bash
# Create a token in database mode first
# ... (re-run Test 2.3 if needed) ...

# Stop server
pkill -f "uvicorn mcpgateway.main:app"

# Switch to Vault backend
sed -i 's/OAUTH_TOKEN_BACKEND=database/OAUTH_TOKEN_BACKEND=vault/' .env

# Start server
make dev &
sleep 5

# Try to retrieve token
curl -s -H "Authorization: Bearer $CF_TOKEN" \
  "http://localhost:4444/gateways/$GATEWAY_ID/tokens?app_user_email=test-user@example.com"

# Expected: 404 or "no tokens found" (database tokens not visible in Vault mode)

# Check Vault
vault kv get "secret/contextforge/oauth/default/$SERVER_ID_HASH/test-user%40example.com" 2>&1

# Expected: Error (database tokens not in Vault)
```

**Expected:**
- Backend switches to Vault
- Database tokens are NOT visible
- User must re-authorize

**✅ Backend Switching: PASS**

---

## SECTION 5: Cleanup

```bash
# Stop ContextForge
pkill -f "uvicorn mcpgateway.main:app"

# Stop Vault
pkill vault

# Remove test data
rm -rf ~/.vault-config/vault-init.txt
sqlite3 mcp.db "DELETE FROM oauth_tokens WHERE app_user_email='test-user@example.com';"

# Clean Vault data in PostgreSQL
psql -U vault_user -d vault_dev -c "DELETE FROM vault_kv_store WHERE path LIKE '%contextforge%';"

echo "✅ Cleanup complete!"
```

---

## 📊 Test Results Summary

**Date:** ___________  
**Tester:** ___________

| Section | Test | Result | Notes |
|---------|------|--------|-------|
| **Section 2: Database Backend** | | | |
| 2.1 | Verify database backend active | ☐ Pass ☐ Fail | |
| 2.2 | Initiate OAuth flow | ☐ Pass ☐ Fail | |
| 2.3 | Complete OAuth flow | ☐ Pass ☐ Fail | |
| 2.4 | Verify token in database | ☐ Pass ☐ Fail | |
| 2.5 | Verify token retrieval | ☐ Pass ☐ Fail | |
| 2.6 | Revoke token | ☐ Pass ☐ Fail | |
| **Section 3: Vault Backend** | | | |
| 3.1 | Switch to Vault backend | ☐ Pass ☐ Fail | |
| 3.2 | Verify Vault backend active | ☐ Pass ☐ Fail | |
| 3.3 | Test /vault/authorize endpoint | ☐ Pass ☐ Fail | |
| 3.4 | Complete Vault OAuth flow | ☐ Pass ☐ Fail | |
| 3.5 | Verify token in Vault (not DB) | ☐ Pass ☐ Fail | |
| 3.6 | Verify Vault path structure | ☐ Pass ☐ Fail | |
| 3.7 | Test token cache | ☐ Pass ☐ Fail | |
| 3.8 | Test multi-gateway server | ☐ Pass ☐ Fail ☐ Skip | |
| 3.9 | Test token revocation | ☐ Pass ☐ Fail | |
| 3.10 | Test error scenarios | ☐ Pass ☐ Fail | |
| **Section 4: Backend Switching** | | | |
| 4.1 | Switch Vault → Database | ☐ Pass ☐ Fail | |
| 4.2 | Switch Database → Vault | ☐ Pass ☐ Fail | |

**Overall Result:** ☐ ALL PASS ☐ SOME FAILURES

**Issues Found:**

1. ________________________________________
2. ________________________________________
3. ________________________________________

---

## 🐛 Common Issues & Solutions

### Issue: Vault won't start
**Solution:** Check PostgreSQL is running and vault_kv_store table exists

### Issue: "VAULT_TOKEN invalid or expired"
**Solution:** Create a new token: `vault token create -policy=contextforge -ttl=24h`

### Issue: 404 "Server not found"
**Solution:** Verify SERVER_ID is correct: `echo $SERVER_ID`

### Issue: OAuth callback shows error
**Solution:** Check GitHub OAuth app callback URL matches: `http://localhost:4444/oauth/callback` or `/vault/callback`

### Issue: Database has 0 tokens but should have some
**Solution:** Check you completed the OAuth flow successfully in browser

### Issue: Vault path not found
**Solution:** Verify server_id hash is correct: `echo -n "https://mcp.github.example.com" | sha256sum | cut -c1-8`

---

## ✅ Sign-off

**Tested by:** _____________  
**Date:** _____________  
**Result:** ☐ READY TO COMMIT ☐ ISSUES FOUND

**Notes:**

---

**End of Manual Testing Guide**
