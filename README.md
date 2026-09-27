# Notes MCP — MCP servers, from a toy to something you can ship

A working Model Context Protocol server built in three stages, where each stage
exposes the problem the next one solves.

| Stage | File | Transport | Who can call it |
|-------|------|-----------|-----------------|
| 1 | `notes_mcp/v1_local.py` | stdio subprocess | only processes on your machine |
| 2 | `notes_mcp/v2_remote.py` | streamable HTTP | **anyone who knows the URL** |
| 3 | `notes_mcp/v3_oauth.py` | streamable HTTP + OAuth 2.1 | authenticated users, scoped per tool, data isolated per user |

Stage 3 is where most tutorials stop short. It is an OAuth 2.1 *resource
server*: it publishes RFC 9728 metadata, answers unauthenticated calls with a
401 that tells the client where to log in, verifies every bearer token against
the authorization server's JWKS (signature, issuer, **audience**, expiry),
enforces a scope per tool, and filters every query by the `sub` claim.

Because an authorization server you cannot run is an authorization server you
cannot test, this repo also ships one: `notes_mcp/dev_auth_server.py` implements
discovery, dynamic client registration, `/authorize` with a consent screen,
PKCE-verified code exchange, JWKS, and refresh-token rotation — about 450 lines,
for local development only.

![The consent screen](article/images/consent-screen.png)

## Quick start

```bash
# 1. Install
curl -fsSL https://astral.sh/uv/install.sh | sh
uv venv && uv pip install -e .
cp .env.example .env

# 2. Stage 1 — local stdio
uv run notes_mcp/v1_local.py

# 3. Stage 2 — remote HTTP (read the warning it prints)
uv run notes_mcp/v2_remote.py          # -> http://127.0.0.1:8000/mcp

# 4. Stage 3 — remote HTTP + OAuth, two terminals
uv run notes_mcp/dev_auth_server.py    # -> http://127.0.0.1:9000
uv run notes_mcp/v3_oauth.py           # -> http://127.0.0.1:8000/mcp
```

## Verify it

Three suites, 58 assertions, no mocks — they talk to the real servers over the
real protocol.

```bash
uv run scripts/verify_basic.py         # 12 checks: stdio + HTTP tools work
uv run scripts/verify_oauth.py         # 18 checks: 401s, metadata, audience, scopes, tenant isolation
uv run scripts/verify_browser_flow.py  # 28 checks: the whole OAuth dance in Chromium
HEADED=1 uv run scripts/verify_browser_flow.py   # watch it click through
```

`verify_browser_flow.py` is the interesting one. It drives the flow a real
client performs, in order: anonymous call → 401 → parse `WWW-Authenticate` →
fetch protected-resource metadata → fetch authorization-server metadata →
dynamically register a client → generate a PKCE pair → open `/authorize` in
Chromium → log in and consent → catch the redirect on a loopback listener →
exchange code + verifier for tokens → call tools → rotate the refresh token.

It also asserts the failure modes: `/authorize` without PKCE is rejected, a
replayed authorization code is rejected, a token minted for a different
audience is rejected, and a user who unticks `notes:write` at the consent
screen gets a token that genuinely cannot write.

## Connecting a client

Configs for Cursor, Claude Desktop, and VS Code are in `clients/`. Replace
`ABSOLUTE_PATH` with the output of `pwd`.

Stage 1 needs a command; stage 3 needs only a URL — the 401 handshake does the
rest, so there is no API key to paste anywhere:

```json
{ "mcpServers": { "notes": { "url": "http://localhost:8000/mcp" } } }
```

## Using a real authorization server

Nothing in `v3_oauth.py` changes. Swap the issuer in `.env`:

```bash
# Descope — hosted login, consent, and dynamic client registration
DESCOPE_CONFIG_URL=https://api.descope.com/v1/apps/P.../.well-known/openid-configuration

# or any other OIDC / OAuth 2.1 provider (Auth0, WorkOS, Keycloak, Clerk, ...)
OAUTH_ISSUER=https://your-tenant.us.auth0.com
OAUTH_AUDIENCE=https://notes.example.com/mcp
```

Then set `MCP_SERVER_URL` to your public URL (including `/mcp`) and remove
`ALLOW_DEV_AUTH`.

## Storage

SQLite by default (`./notes.db`), no setup. For Postgres:

```bash
uv pip install -e ".[postgres]"
DATABASE_URL=postgresql://user:pass@localhost:5432/notes
```

Every row carries an `owner_id`, and every read and delete filters on it. That
predicate is the entire multi-tenancy story — remove it and any authenticated
user can read and delete everyone else's notes by guessing ids.

## Layout

```
notes_mcp/
  notes_db.py         storage, SQLite + Postgres, owner-scoped
  auth.py             standalone resource-server logic (discovery, JWKS, scopes)
  v1_local.py         stage 1 — stdio
  v2_remote.py        stage 2 — HTTP, no auth
  v3_oauth.py         stage 3 — HTTP + OAuth 2.1 + scopes + per-user data
  dev_auth_server.py  a local OAuth 2.1 authorization server (dev only)
scripts/
  verify_basic.py         stage 1 and 2
  verify_oauth.py         resource-server behaviour
  verify_browser_flow.py  full browser OAuth flow via Playwright
  publish_to_medium.py    push the article to Medium with Playwright
clients/                configs for Cursor, Claude Desktop, VS Code
article/                the write-up and its screenshots
```

## Security notes

- **Audience validation is not optional.** A token is a bearer credential; if
  you do not check that it was minted for *your* resource, any server your user
  logs into can replay their token against you. That is the confused-deputy
  problem the MCP spec calls out.
- **Scope checks belong in the tool body too.** Hiding a tool from the list is a
  UX affordance, not a control — a client can still call it by name. Both layers
  are implemented here.
- **Never key data on email.** Users change it, and some providers issue it
  unverified. Use `sub`.
- **Don't leak existence.** `get_note` returns the same "not found" whether the
  id is absent or owned by someone else.
- **`dev_auth_server.py` is not production software.** Ephemeral in-memory keys,
  no password check, no rate limiting. Delegate this to a real provider.

## Publishing the article to Medium

Medium retired its publishing API in 2023, so `scripts/publish_to_medium.py`
drives the real editor with Playwright.

```bash
uv pip install -e ".[publish]" && playwright install chromium

uv run scripts/publish_to_medium.py --login      # one-time, opens a browser
uv run scripts/publish_to_medium.py --dry-run    # parse only, no browser
uv run scripts/publish_to_medium.py              # create a draft
uv run scripts/publish_to_medium.py --publish    # draft + publish (asks to confirm)
```

The markdown converter handles headings, code blocks, dividers, lists,
blockquotes, and image uploads. Defaults are safe: it stops at a draft, and
`--publish` still requires typing `publish` at a prompt.

### What the editor actually needs (found by probing, not guessing)

These cost a few hours to discover and are the reason the script works:

- **Code blocks are `Cmd/Ctrl+Alt+6`**, not `Alt+K`. Typing ``` ``` ``` does not
  autoconvert.
- **To leave a code block**, press Enter once and then *click* the trailing
  paragraph. Toggling the shortcut off collapses the block back into prose, and
  pressing Enter twice leaves the caret inside.
- **Typing `---` does not create a divider** — it stays literal text. Use the
  toolbar's `inline-menu-hr` button.
- **The `+` toolbar buttons are always in the DOM but invisible** until the
  parent `inline-menu` toggle is clicked, and that toggle only appears on an
  **empty** paragraph. A divider or image after prose needs a blank line first.
- **Lists continue automatically on Enter**, so only the first item in a run
  should type its `- ` marker.
- **Headings and quotes persist to the next block**; reset with `Cmd/Ctrl+Alt+0`.
- **Headless Chromium gets a 403** from Medium's bot protection. Run headed.
- **`**bold**` markers are typed literally** by the editor, so they are stripped
  before typing; inline `` `code` `` is converted by Medium and is kept.

`.medium-session.json` and `.medium-profile/` hold live credentials and are
gitignored. Treat them like passwords.
