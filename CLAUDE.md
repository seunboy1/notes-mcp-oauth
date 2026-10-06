# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**notes-mcp** is a Model Context Protocol server built in three progressive stages, demonstrating the evolution from a local toy to a production-ready OAuth-protected resource server. Each stage exposes the problems the next one solves, making it an educational resource for understanding MCP servers and OAuth 2.1 security.

| Stage | Module | Transport | Access | Use Case |
|-------|--------|-----------|--------|----------|
| 1 | `v1_local.py` | stdio subprocess | local processes | development, education |
| 2 | `v2_remote.py` | HTTP (no auth) | anyone with URL | demonstration of security gap |
| 3 | `v3_oauth.py` | HTTP + OAuth 2.1 | authenticated, scoped users | production-ready |

## Setup & Development Commands

### Initial Setup
```bash
# Install uv (Python package manager)
curl -fsSL https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
uv venv && uv pip install -e .

# Copy and configure environment
cp .env.example .env
```

### Running the Three Stages

**Stage 1 - Local stdio (testing your tools locally)**
```bash
uv run notes_mcp/v1_local.py
```

**Stage 2 - Remote HTTP without auth (demonstrates security gap)**
```bash
uv run notes_mcp/v2_remote.py  # Server listens at http://127.0.0.1:8000/mcp
```

**Stage 3 - Remote HTTP + OAuth 2.1 (production-ready)**
```bash
# Terminal 1: Start the bundled dev authorization server
uv run notes_mcp/dev_auth_server.py  # http://127.0.0.1:9000

# Terminal 2: Start the MCP resource server
uv run notes_mcp/v3_oauth.py  # http://127.0.0.1:8000/mcp
```

### Verification & Testing

```bash
# Run all verification tests (58 assertions, no mocks - hit real servers)
uv run scripts/verify_basic.py                            # 12 checks: stdio + HTTP tools work
uv run scripts/verify_oauth.py                             # 18 checks: 401s, metadata, audience, scopes, tenant isolation
python3 ~/.claude/scripts/verify_browser_flow.py           # 28 checks: full OAuth dance in Chromium
HEADED=1 python3 ~/.claude/scripts/verify_browser_flow.py  # Watch the browser automate the flow
```

`verify_browser_flow.py` is not a project file — see [Shared Playwright Scripts](#shared-playwright-scripts) below.

### Media Publishing

Both scripts below are not project files — see [Shared Playwright Scripts](#shared-playwright-scripts).

**Medium (article publishing)**
```bash
# Install playwright once, in whatever Python runs these scripts
python3 -m pip install playwright && playwright install chromium

python3 ~/.claude/scripts/publish_to_medium.py --login      # One-time authentication
python3 ~/.claude/scripts/publish_to_medium.py --dry-run    # Parse only, no browser
python3 ~/.claude/scripts/publish_to_medium.py              # Create a draft
python3 ~/.claude/scripts/publish_to_medium.py --publish    # Draft + publish (asks to confirm)
```

**LinkedIn (post drafting)**
```bash
python3 ~/.claude/scripts/post_to_linkedin.py --login                      # One-time auth
python3 ~/.claude/scripts/post_to_linkedin.py --dry-run --post linkedin/mcp-oauth-post.md
python3 ~/.claude/scripts/post_to_linkedin.py --post linkedin/mcp-oauth-post.md
python3 ~/.claude/scripts/post_to_linkedin.py --post linkedin/mcp-oauth-post.md --document path/to/carousel.pdf --doc-title "Title"
```

### Optional: PostgreSQL Backend
```bash
# Default is SQLite (./notes.db)
uv pip install -e ".[postgres]"
DATABASE_URL=postgresql://user:pass@localhost:5432/notes uv run notes_mcp/v3_oauth.py
```

## Architecture & Key Modules

### Core MCP Server Modules (`notes_mcp/`)

**`auth.py`** - OAuth 2.1 resource server logic (provider-agnostic)
- Advertises authorization server metadata via `/.well-known/oauth-protected-resource`
- Verifies bearer tokens: signature validation, issuer check, audience check, expiry check
- Scope enforcement at tool definition and execution time
- Derives caller identity from the `sub` (subject) JWT claim
- Works with any compliant OIDC/OAuth 2.1 provider (Descope, Auth0, Keycloak, etc.)

**`notes_db.py`** - Storage abstraction layer
- Two backends: SQLite (default, zero setup) and PostgreSQL (production)
- Every row is scoped to an `owner_id`: the literal string `"local"` in stages 1-2, or the JWT `sub` claim in stage 3
- Core security: filtering every read/delete by owner prevents data leakage between tenants
- `NoteNotFound` exception never reveals whether a note exists for another user

**`v1_local.py`** - Stage 1: Local stdio MCP server
- Tools communicate via JSON-RPC over subprocess stdin/stdout
- No network, no auth — purely for local testing
- Simplest way to verify tool logic works

**`v2_remote.py`** - Stage 2: Remote HTTP without auth
- Demonstrates HTTP transport using FastMCP and Uvicorn
- Shows the security problem: accessible to anyone who knows the URL
- No token validation, no scopes, no tenant isolation

**`v3_oauth.py`** - Stage 3: Production-ready OAuth resource server
- Adds JWT verification and scope enforcement
- Implements per-user data filtering via the `sub` claim
- Tools declare their required scopes; tokens without them are rejected
- Publishes RFC 9728 `.well-known` metadata and returns 401 with `WWW-Authenticate` headers

**`dev_auth_server.py`** - Bundled authorization server for local development
- Full OAuth 2.1 flow: discovery, dynamic client registration, `/authorize` with consent screen, PKCE-verified code exchange, JWKS, refresh token rotation
- ~450 lines, ephemeral in-memory keys, no password validation
- **Development only** — never use in production

### Verification Scripts (`scripts/`)

**`verify_basic.py`** - Tests stages 1 and 2 (stdio + HTTP)
- Verifies tools work over both transports
- No OAuth, no scopes

**`verify_oauth.py`** - Tests OAuth resource server behaviors
- Verifies 401 with proper headers on unauthenticated calls
- Validates token signature, issuer, audience, and expiry
- Tests scope enforcement (token without `notes:write` cannot delete)
- Tests tenant isolation (users cannot see each other's notes by ID guessing)

**`verify_browser_flow.py`** (in `~/.claude/scripts/`, not this repo) - End-to-end OAuth flow automation via Playwright
- Simulates what a real client (Claude Desktop, VS Code, etc.) performs
- Anonymous call → 401 → parse metadata → register client → PKCE pair → authorize in browser → catch redirect → exchange code → call tools → rotate refresh token
- Also asserts failure modes: replayed codes rejected, wrong audience rejected, scope filtering works
- Imports `notes_mcp` from whichever directory it's invoked from, so run it from this repo's root

### Media Publishing (`~/.claude/scripts/`, `linkedin/`, `article/`)

See [Shared Playwright Scripts](#shared-playwright-scripts) — none of the scripts below are project files.

**`publish_to_medium.py`** - Automates Medium article editing
- Medium deprecated its API in 2023; this script drives the real editor with Playwright
- Handles Markdown → Medium format conversion (headings, code blocks, dividers, lists, blockquotes, image uploads)
- Discovered non-obvious editor requirements (e.g., code blocks are `Cmd/Ctrl+Alt+6`, not triple backticks)

**`post_to_linkedin.py`** - Drafts LinkedIn posts with automated composer interaction
- Takes markdown text, an optional image, or a PDF via `--document` (attached as a native LinkedIn document, not an image)
- Key insight: LinkedIn counts UTF-16 code units, not characters; astral unicode costs 2 units each
- Navigates feed, attaches media, saves as draft (publish is manual)

**`html_to_pdf.py`** - Renders any HTML file to PDF via headless Chromium
- Honors the HTML's own `@page { size: ... }` by default (`prefer_css_page_size`), rather than forcing Letter/A4 — needed for fixed-pixel-dimension designs like the LinkedIn carousels in `linkedin/`
- `html_to_pdf.py slides.html [out.pdf] [--format A4]`

## Configuration

All configuration comes from `.env` (copy from `.env.example` and edit):

**Server endpoints:**
- `MCP_SERVER_URL` — Public URL where this server is reachable (e.g., `http://localhost:8000/mcp`), used as OAuth audience
- `HOST`, `PORT` — Where the HTTP server listens

**Storage:**
- `DATABASE_URL` — Leave empty for SQLite (default: `./notes.db`), or set to PostgreSQL connection string
- `SQLITE_PATH` — SQLite file location (default: `./notes.db`)

**Authorization server (choose ONE):**
- `ALLOW_DEV_AUTH=true` + `DEV_AUTH_URL` — Use bundled dev server (development only)
- `OAUTH_ISSUER` + `OAUTH_AUDIENCE` — Use any OIDC provider (Auth0, Keycloak, etc.)
- `DESCOPE_CONFIG_URL` — Use Descope Agentic Identity Hub

**OAuth validation:**
- `MCP_SCOPES` — Comma-separated scopes (default: `notes:read,notes:write`)
- `OAUTH_VERIFY_AUDIENCE` — Validate token audience (default: `true`; disable only for debugging)

## Security Design Decisions

**Audience validation is not optional.** Tokens are bearer credentials; if you don't verify they were minted for *your* resource, any server your user logs into can replay their token. This is the "confused deputy" problem the MCP spec calls out.

**Scope checks belong in two places:** At tool definition time (UX — hide tools users can't use) and in tool execution (security — a client can still call by name even if hidden).

**Use `sub` claim, never email.** Users change emails; some providers issue them unverified. The `sub` (subject) claim is stable and unique per provider.

**Don't leak data existence.** `get_note` returns "not found" (404) whether the note id doesn't exist or belongs to another user. Distinguish only on success.

**Every read/delete is filtered by owner.** The single `owner_id` column is the entire multi-tenancy story. Remove it and any authenticated user can read/delete everyone else's notes.

## Connecting Clients

Configs for Cursor, Claude Desktop, and VS Code are in `clients/`. Replace `ABSOLUTE_PATH` with the output of `pwd`.

**For stage 1** (local stdio), clients need a `command`:
```json
{
  "mcpServers": {
    "notes": {
      "command": "uv",
      "args": ["run", "ABSOLUTE_PATH/notes_mcp/v1_local.py"]
    }
  }
}
```

**For stages 2 and 3** (HTTP), clients only need a URL — the 401 handshake handles the rest:
```json
{
  "mcpServers": {
    "notes": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

## Using a Real Authorization Server

Nothing in `v3_oauth.py` changes. Swap the issuer in `.env`:

**Descope** (hosted login, consent, dynamic client registration):
```bash
DESCOPE_CONFIG_URL=https://api.descope.com/v1/apps/P.../.well-known/openid-configuration
```

**Any OIDC/OAuth 2.1 provider** (Auth0, WorkOS, Keycloak, Clerk, Supabase, etc.):
```bash
OAUTH_ISSUER=https://your-tenant.us.auth0.com
OAUTH_AUDIENCE=https://notes.example.com/mcp
```

Then set `MCP_SERVER_URL` to your public URL and remove `ALLOW_DEV_AUTH`.

## Repository Layout

```
notes_mcp/
  notes_db.py         storage layer (SQLite + Postgres, owner-scoped)
  auth.py             OAuth 2.1 resource server logic (provider-agnostic)
  v1_local.py         stage 1 — stdio
  v2_remote.py        stage 2 — HTTP, no auth
  v3_oauth.py         stage 3 — HTTP + OAuth 2.1 + scopes + per-user data
  dev_auth_server.py  local OAuth 2.1 authorization server (dev only)

scripts/
  verify_basic.py         test stages 1 and 2 (12 checks)
  verify_oauth.py         test OAuth resource server (18 checks)

clients/                config examples for Cursor, Claude Desktop, VS Code
article/                write-up and screenshots for the Medium article
linkedin/               LinkedIn post drafts, carousel HTML/PDF, and supporting images
```

Playwright-driven scripts (`verify_browser_flow.py`, `publish_to_medium.py`,
`post_to_linkedin.py`, `delete_medium_drafts.py`, `html_to_pdf.py`) live in
`~/.claude/scripts/`, not in this project's `scripts/` directory — see
[Shared Playwright Scripts](#shared-playwright-scripts).

## Shared Playwright Scripts

The five Playwright-driven scripts above are general-purpose helpers, not
project files — they live in `~/.claude/scripts/` and are usable from any
project directory. Each resolves its working files (session files, `article/`,
`linkedin/`, etc.) relative to **the directory it is invoked from** (`Path.cwd()`),
not its own location, so always invoke them from this repo's root:

```bash
cd /path/to/mcp-server-oauth
python3 ~/.claude/scripts/post_to_linkedin.py --login
```

`delete_medium_drafts.py` loads `publish_to_medium.py` as a sibling module by
file path (`importlib`), so the two must stay in the same folder as each
other, wherever that folder is.

## Key Learnings & Patterns

**Why three stages exist:** Each stage teaches a specific lesson. Stage 1 proves the tool logic works locally. Stage 2 shows that HTTP alone is not secure. Stage 3 shows how proper authentication and scoping solve the problem.

**Token verification is provider-agnostic:** The same `JWTVerifier` works with any OIDC provider. The only config is the issuer and JWKS endpoint.

**Refresh token rotation:** OAuth servers issue both access and refresh tokens. Refresh tokens are long-lived and let clients get new access tokens after expiry. The dev auth server demonstrates this; production servers rotate refresh tokens on each exchange.

**PKCE is not optional for user-facing clients:** Proof Key for Code Exchange prevents authorization-code interception. `verify_browser_flow.py` generates a PKCE pair and validates it's checked.

**Playwright is the right tool for bot protection:** Headless Chromium gets 403 from Medium. Running with `HEADED=1` works because the browser looks real.

**LinkedIn counts UTF-16, not characters:** Unicode headers using astral characters (like bold math alphanumerics `𝗟𝗶𝗸𝗲`) count as 2 units each. The `li_len()` function is the only count that matches the composer.

## Development Notes

- `notes.db` (SQLite) is gitignored by default
- `.env` is gitignored; `.env.example` is tracked
- `.medium-session.json` and `.linkedin-session.json` are gitignored credentials
- `.medium-profile/` and `.linkedin-profile/` contain session state and are gitignored
- `linkedin/shots/` contains automation screenshots and is gitignored
- All verification tests are read-only and safe to run repeatedly
