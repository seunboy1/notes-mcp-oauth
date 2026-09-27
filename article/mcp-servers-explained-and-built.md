# MCP Servers, Explained Properly — And Built With Auth That Actually Holds

*A working Notes server in three stages, plus the local OAuth 2.1 authorization server that lets you test the part everyone skips.*

---

There is a version of the MCP explainer you have already read. It defines the acronym, builds a server with three tools, connects it to Claude or Cursor, and stops. Sometimes it adds a line about "and of course in production you'd add authentication," which is a bit like a flight manual that ends with "and of course in production you'd add landing."

Authentication is not an appendix to a remote MCP server. It is the part that decides whether your server is software or a liability. So this piece does two things the short version doesn't:

1. Explains what MCP actually is, precisely, including the bits that the "USB-C for AI" analogy quietly drops.
2. Builds the server three times — local, remote, and remote-with-real-OAuth — and **proves** each stage works with 58 assertions that run against the real servers over the real protocol, including the full authorization flow driven through a real browser.

Along the way we build a complete OAuth 2.1 authorization server that runs on localhost, because an auth flow you cannot run end-to-end on your laptop is an auth flow you will not test, and an untested auth flow is a guess.

Everything here runs. Every number is from a suite you can execute yourself.

---

## Part 1: What an MCP server actually is

Start with the constraint that makes all of this necessary: **a language model cannot do anything.** It maps text to text. It cannot read your files, query your database, or check your calendar. That is not a limitation to be engineered around; it is the whole security model, and it is worth keeping.

So we give models tools. A tool is a function with a name, a description, and a typed signature. The model reads that list and, when it wants one, emits text saying so. **Your code** parses that, executes the function, and feeds the result back as more text.

The model never calls anything. It requests. Your code decides.

Hold onto that, because it determines where security lives. The model is not a threat actor you need to authenticate — it is a text generator making suggestions. The threat actor is whoever can reach the endpoint that *acts on* those suggestions. Authentication belongs there, not in the prompt.

### The problem MCP solves

Before MCP, every application built this plumbing itself, differently. A GitHub integration written for one client did not work in another. Each new agent meant rewriting the same adapters — an N×M problem, N clients times M tools, and both were growing.

MCP is the standard that collapses it: write your tools once as an MCP **server**, and any **client** that speaks MCP can use them.

The USB-C analogy is popular and roughly right, but it hides the thing that matters most. A USB-C cable does not need to know who is holding the other end. A remote MCP server absolutely does.

### The two sides

- **Server** — your code. It exposes tools.
- **Client** — the application the model lives in: Claude Desktop, Cursor, VS Code, your own agent.

On connect, the client asks *what do you have?* The server returns a list of names, descriptions, and schemas, which the client hands to the model. When the model wants one, the client sends a JSON-RPC request; the server runs the function and returns the result. Back and forth, all of it JSON.

### Two ways to run

**Local (stdio).** The client launches your server as a subprocess and pipes JSON-RPC over stdin/stdout. No port, no network, no credentials — the OS process boundary *is* the security model. Excellent for personal tools. Useless for anyone else.

**Remote (streamable HTTP).** Your server has a URL. Any client that can reach it, can use it. This is how every real product ships MCP, and it is where the interesting problem appears.

---

## Part 2: The problem with a URL

The moment your server has a URL, it inherits a question it never had to answer as a subprocess: **who is calling?**

Your tools *do things*. They read data, write data, delete data. A naked URL means every one of those operations is available to everyone who learns the URL. And URLs leak — config files, screenshots, shell history, support tickets, logs.

So every request needs three answers:

1. **Who is calling?** (authentication)
2. **What are they allowed to do?** (authorization)
3. **On whose behalf?** (identity, for data isolation)

The common answer is a static API key in a config file. It fails all three, and it fails in a way that gets worse as you succeed. One shared secret cannot distinguish five agents. You cannot tell which user's agent did what. And when one is compromised you cannot revoke it alone — you rotate the key and break every integration simultaneously.

The MCP specification's answer is the one the rest of the industry settled on years ago: **OAuth 2.1 with PKCE**. You will not implement most of it, but you must understand its shape, because you implement the half that lives in your server.

### The flow, and the one line that matters

**Your MCP server never sees a password.** It is an OAuth *resource server*. It validates tokens; it does not issue them. That separation is the design.

Concretely:

1. A client calls your server with no token. You answer **401**, plus a `WWW-Authenticate` header pointing at your metadata.
2. The client fetches that metadata and learns which **authorization server** to talk to.
3. The client **registers itself** — dynamically, no human, no dashboard (RFC 7591).
4. The client generates a PKCE pair and opens `/authorize` in a browser.
5. The user logs in and sees a consent screen listing the requested **scopes**.
6. On approval, the browser redirects back with a short-lived **authorization code**.
7. The client exchanges that code — plus its PKCE verifier, which proves it is the same client that started the flow — for an **access token**.
8. The client sends that token on every subsequent request.
9. Your server verifies the signature, issuer, audience, and expiry, checks scopes, and runs the tool.

Note what the user never does: paste a key. Note what you never store: a long-lived secret.

Two terms carry the weight:

**Scopes** are the permissions inside the token — `notes:read`, `notes:write`. Tools declare what they need; tokens carry what was granted. This is how a read-only agent is *structurally* unable to delete, rather than merely discouraged from it.

**The `sub` claim** is the stable user id. It is what makes one deployment serve many users without leaking between them.

---

## Part 3: Stage 1 — local, over stdio

Enough theory. Storage first, because one design decision here determines whether stage 3 is possible.

```python
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT NOT NULL,      -- <- the entire multi-tenancy story
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
)
```

Every row is owned. Every read and delete filters on `owner_id`:

```python
def delete_note(note_id: int, owner_id: str) -> bool:
    """Delete a note the caller owns. Returns False if nothing matched."""
    cur = conn.execute(
        "DELETE FROM notes WHERE id = ? AND owner_id = ?",
        (note_id, owner_id),
    )
    return cur.rowcount > 0
```

That second predicate is not a nicety. Drop it and any authenticated user can delete anyone's data by guessing integers. Add it on day one, even while `owner_id` is hardcoded to `"local"`, and stage 3 becomes a change of variable rather than a migration.

Now the server:

```python
from fastmcp import FastMCP

mcp = FastMCP(
    name="notes-local",
    instructions=(
        "A personal notes store. Use list_notes to see what exists before "
        "adding or deleting, and always echo the note id back to the user."
    ),
)

@mcp.tool
def add_note(
    content: Annotated[str, Field(min_length=1, max_length=10_000)],
) -> dict[str, Any]:
    """Save a new note and return the created note, including its new id."""
    return {"ok": True, "note": notes_db.add_note(content, LOCAL_OWNER)}

if __name__ == "__main__":
    notes_db.init_db()
    mcp.run()   # stdio: the client owns this process's lifetime
```

Three details that disproportionately affect quality:

**The docstring is the tool description the model reads.** It is not documentation for your colleagues; it is a prompt. "Save a new note and return the created note, including its new id" tells the model what comes back. "Adds a note" does not.

**Type annotations become the JSON Schema** the model plans against. `Annotated[str, Field(max_length=10_000)]` means the client can reject a malformed call before it reaches you.

**Server-level `instructions` set policy.** This is where you say *check before you delete* once, instead of hoping each tool description implies it.

Point Cursor or Claude Desktop at it with a command, not a URL:

```json
{
  "mcpServers": {
    "notes-local": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/Mcp-server", "notes_mcp/v1_local.py"]
    }
  }
}
```

Verified — a real MCP client, a real subprocess:

```
Stage 1 - local stdio server
  PASS  stdio: client connected to the subprocess (no port, no token)
  PASS  stdio: tools discovered -> ['add_note', 'delete_note', 'list_notes']
  PASS  stdio: add_note -> id=1
  PASS  stdio: list_notes contains the new note (count=1)
  PASS  stdio: delete_note -> ok
  PASS  stdio: deleting a bogus id fails cleanly
```

---

## Part 4: Stage 2 — remote, and why that is alarming

Going remote is one line:

```python
mcp.run(transport="http", host=HOST, port=PORT)
```

That is it. Same tools, same code, now reachable at `http://127.0.0.1:8000/mcp`. Any client on the network can connect — phone, laptop, CI, colleague, stranger.

The same suite passes against it, and one line is the point of the whole article:

```
Stage 2 - remote streamable HTTP, no auth
  PASS  http: connected to http://127.0.0.1:64138/mcp with NO credentials at all
  PASS  http: tools discovered -> ['add_note', 'delete_note', 'list_notes']
  PASS  http: add_note -> id=2
  PASS  http: delete_note -> ok
```

**No credentials at all.** No token, no key, no header. The test client asked for the tool list and got it, then wrote data, then deleted data. `delete_note` is as reachable as `list_notes`.

And there is a second, quieter failure: every caller shares one bucket. There is no notion of *your* notes. Two users are one user.

So the server prints this on boot, in the code as shipped:

```
==========================================================================
  notes-remote is running WITHOUT authentication.

  Every caller shares one notes bucket ("local") and every tool -
  including delete_note - is reachable by anyone who can open the URL.

  Fine on 127.0.0.1. Never expose this to the internet.
  Use notes_mcp/v3_oauth.py for anything real.
==========================================================================
```

---

## Part 5: Stage 3 — a real resource server

Three things change. None of them is large; all of them matter.

### 1. The server becomes an OAuth resource server

```python
verifier = JWTVerifier(
    jwks_uri=f"{issuer}/.well-known/jwks.json",
    issuer=issuer,
    audience=audience,        # <- this line stops token replay
    base_url=BASE_URL,
)

auth = RemoteAuthProvider(
    token_verifier=verifier,
    authorization_servers=[issuer],
    base_url=BASE_URL,
    scopes_supported=["notes:read", "notes:write"],
    resource_name="Notes MCP",
)

mcp = FastMCP(name="notes", auth=auth)
```

That publishes the RFC 9728 metadata document, returns a `WWW-Authenticate` challenge on unauthenticated calls, and validates every bearer token against the authorization server's public keys.

**Do not skip `audience`.** A bearer token is like cash: whoever holds it can spend it. If you do not verify that a token was minted *for your server*, then any other server your user authenticates to can replay their token against you — and your server will cheerfully act on their behalf. This is the confused-deputy problem, and the MCP spec calls it out explicitly. It is one parameter. Set it.

We verify the negative case rather than assuming it:

```
3. A token for another audience is rejected
  PASS  token with foreign audience -> 401 (got 401)
```

### 2. Tools declare the scope they need

```python
@mcp.tool(auth=require_scopes(NOTES_WRITE))
def delete_note(note_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
    """Permanently delete one of the signed-in user's notes.

    Destructive. Confirm the id with the user first. Requires notes:write,
    and silently refuses ids the caller does not own.
    """
    principal = require_scope(NOTES_WRITE)          # second, independent check
    if not notes_db.delete_note(note_id, principal["user_id"]):
        return {"ok": False, "error": f"no note with id {note_id}"}
    return {"ok": True, "deleted_id": note_id}
```

Two checks, deliberately. The decorator hides the tool from under-privileged callers — good UX, and it keeps irrelevant tools out of the model's context. But hiding a tool is not a control: a client can still call it by name. So the body checks again, right next to the data access. Defence in depth means the check is at the boundary it protects, not one layer away.

### 3. Identity comes from the token

```python
def current_principal() -> dict[str, Any]:
    token = get_access_token()
    if token is None:
        raise ToolError("not authenticated")

    claims = getattr(token, "claims", None) or {}
    user_id = getattr(token, "subject", None) or claims.get("sub")
    if not user_id:
        raise ToolError("token carries no subject - cannot scope data to a user")

    return {"user_id": str(user_id), "scopes": sorted(token.scopes or []), ...}
```

Then `principal["user_id"]` flows into every query, and the `owner_id` predicate from Part 3 does the rest.

**Key on `sub`, never on email.** Users change email addresses, and some providers will hand you one that was never verified. `sub` is stable and provider-issued. An email is a display string.

One more detail worth stealing — `get_note` returns the identical error whether the id does not exist or belongs to someone else:

```python
except NoteNotFound:
    # Deliberately indistinguishable from "exists but belongs to someone
    # else" - do not leak the existence of other users' rows.
    return {"ok": False, "error": f"no note with id {note_id}"}
```

Distinguishing those two cases turns your error messages into an enumeration oracle.

---

## Part 6: The authorization server you can actually run

Here is the practical trap. To test any of the above you need an authorization server. Sign up for a hosted provider and you are debugging your token validation against a black box, over the network, with a browser in the loop. Most people write the code, see a green checkmark in a client, and never test the failure modes at all.

So this project ships one: **a complete OAuth 2.1 + PKCE authorization server, roughly 450 lines, running on localhost.**

```
GET  /.well-known/oauth-authorization-server   discovery (RFC 8414)
GET  /.well-known/jwks.json                    public keys
POST /register                                 dynamic client registration (RFC 7591)
GET  /authorize                                login + consent screen
POST /authorize/consent                        the user's decision
POST /token                                    code -> token, refresh -> token
POST /revoke                                   revocation
```

It signs RS256 tokens with an ephemeral in-memory key, enforces PKCE, expires and single-uses authorization codes, rotates refresh tokens, and renders a real consent screen with a checkbox per scope — which the user can untick.

It is emphatically **not** production software: keys vanish on restart, "login" is a name in a text box, no password is checked, no rate limiting, no audit log. That is the point. In production you delegate this entire file to a provider whose job it is — Descope, Auth0, WorkOS, Keycloak, Clerk. Locally, you own the whole flow and can break it on purpose.

![The consent screen](article/images/consent-screen.png)

Notice what the consent screen shows: the client's name and its dynamically issued id, the identity being used, and each requested scope with a plain-English gloss and its own checkbox. That last part is not decoration — untick `notes:write` and the token that comes back cannot write. We test exactly that below.

---

## Part 7: Proving it, in a real browser

This is the part that does not appear in tutorials, because it is the part that is annoying to automate. So let's automate it. Playwright plays the human.

```python
async def consent_in_browser(authorize_url, username, untick=None):
    """Let Playwright be the human: log in, adjust scopes, click Authorize."""
    async with CallbackServer() as callback:       # a real loopback listener
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not HEADED)
            page = await browser.new_page()
            await page.goto(authorize_url, wait_until="domcontentloaded")

            await page.fill("#username", username)
            for scope in untick or []:             # the user says no to a scope
                await page.locator(
                    f'input[name="scope"][value="{scope}"]'
                ).uncheck()

            await page.click('button[value="allow"]')
            return await asyncio.wait_for(callback.received, timeout=20)
```

The redirect is caught by an actual socket bound to `127.0.0.1`, because that is what desktop MCP clients do — bind a loopback port, register it as the `redirect_uri`, wait for the browser to deliver the code. Intercepting the request inside the browser would have been easier and would have tested less.

Here is the whole flow, executed:

```
1. Anonymous call to the MCP server
  PASS  401 Unauthorized (got 401)
  PASS  server told us where to find its metadata

2. Discover the authorization server
  PASS  metadata URL parsed from the header: .../.well-known/oauth-protected-resource/mcp
  PASS  authorization server = http://127.0.0.1:9000
  PASS  authorization server advertises PKCE S256
  PASS  authorization server supports dynamic client registration

3. Reject an authorize request that omits PKCE
  PASS  authorize without code_challenge -> 400 (got 400)

4. Register this client dynamically
  PASS  client_id issued without human setup: dev_00ccd03026c64ce6b04190fa87506198

5. Full code flow in a real browser (alice, both scopes)
        browser is on: 'Sign in - Notes MCP (dev)'
        redirect delivered to http://127.0.0.1:7777/oauth/callback
  PASS  state parameter round-tripped intact
  PASS  code + verifier exchanged for an access token
  PASS  refresh token issued
  PASS  granted scopes = ['notes:read', 'notes:write', 'offline_access']

6. The access token works against the MCP server
  PASS  initialize -> 200 (got 200)
  PASS  tools available: ['add_note', 'delete_note', 'get_note', 'list_notes', 'whoami']
  PASS  whoami -> user_2bd806c97f0e00af / alice@example.dev
  PASS  add_note -> id=2

7. Replaying the same authorization code fails
  PASS  reused code -> 400 (got 400)

8. Refresh rotates the token and the new one still works
  PASS  refresh -> 200 (got 200)
  PASS  a new access token was issued
  PASS  the refresh token was rotated
  PASS  refreshed token is accepted
  PASS  same user, same notes after refresh (count=1)

9. User unticks notes:write at the consent screen
        user unticked scope: notes:write
  PASS  token granted only ['notes:read']
  PASS  write tools hidden: ['get_note', 'list_notes', 'whoami']
  PASS  add_note refused: "Unknown tool: 'add_note'"
  PASS  carol sees none of alice's notes

28/28 checks passed
```

Read step 9 again. A user unticked one checkbox in a browser, and the resulting token could not write — not by convention, not by a code review rule, but because the scope was absent from the token and the server checked. That is what authorization is supposed to feel like.

And step 7 matters more than it looks: authorization codes are single-use and short-lived, so a code captured from a log or a referrer is worthless once redeemed.

A separate suite covers tenant isolation directly — no browser, just two tokens:

```
5. Notes are private per user
  PASS  whoami -> user_2bd806c97f0e00af scopes=['notes:read', 'notes:write']
  PASS  alice wrote note id=1
  PASS  alice sees her note (count=1)
  PASS  bob cannot see alice's note (bob has 0)
  PASS  bob cannot delete alice's note: {'ok': False, 'error': 'no note with id 1'}
  PASS  alice's note survived bob's delete attempt
  PASS  alice can delete her own note
```

Bob is fully authenticated with both scopes. He still cannot touch Alice's data, and the error he receives tells him nothing about whether that note exists. **Authentication is not authorization, and authorization is not data isolation.** Three distinct properties; three distinct tests.

Totals: **12 + 18 + 28 = 58 assertions**, all against live servers over the real protocol.

---

## Part 8: Going to production

The pleasant surprise: none of the tool code changes. Swap the issuer.

```bash
# Descope — hosted login, consent, and dynamic client registration
DESCOPE_CONFIG_URL=https://api.descope.com/v1/apps/P.../.well-known/openid-configuration

# or any other OIDC / OAuth 2.1 provider
OAUTH_ISSUER=https://your-tenant.us.auth0.com
OAUTH_AUDIENCE=https://notes.example.com/mcp
```

Because the server only ever validates a signature, an issuer, an audience, an expiry, and a scope list, the provider is a configuration value. That is the payoff of being a *resource* server rather than rolling your own login.

A short pre-flight list:

- **`MCP_SERVER_URL` must be your public URL, including `/mcp`.** It is both your OAuth resource identifier and your token audience. Get it wrong and every token is rejected — or, worse, tokens for other resources are accepted.
- **HTTPS, obviously.** Bearer tokens over plaintext are not tokens, they are announcements.
- **Keep token lifetimes short** and lean on refresh rotation. An hour is generous.
- **Log the `sub` and `jti`, never the token.** You want to answer "what did this user's agent do?" without your logs becoming a credential store.
- **Scope per tool, not per server.** A read-only integration should be issued a read-only token; that is only meaningful if your tools actually differentiate.
- **Treat destructive tools as destructive.** Say so in the docstring, require the stronger scope, and make the tool idempotent where you can.

### Acting on behalf of users, elsewhere

The natural next step: your tool needs to hit the user's GitHub or Slack. Do not store those tokens yourself. Providers like Descope offer a connections vault that holds each user's third-party grants, so your server requests a short-lived token for a specific user and provider at call time. Your database never contains a long-lived key for a service you do not own — which is the difference between a breach that is embarrassing and one that is somebody else's incident too.

---

## The shape of it

An MCP server is a list of functions a model is allowed to request. That framing is correct, and it is why the acronym is easy.

What is not easy — and what determines whether you ship — is everything that follows from giving those functions a URL:

- **stdio** needs no auth, because the OS provides it.
- **HTTP without auth** is a public API with a delete button.
- **HTTP with a static key** cannot distinguish callers, cannot scope them, and cannot be revoked individually.
- **HTTP with OAuth 2.1** answers who, what, and on whose behalf — and costs about forty lines, most of which is configuration.

The forty lines are not the hard part. The hard part is that you cannot tell whether they work by looking at them. A 401 that fires, a metadata document that resolves, an audience check that actually rejects, a scope that genuinely blocks a write, a tenant boundary that holds under a deliberate attempt to cross it — those are claims, and claims want tests.

So write the tests. Run a local authorization server and break it on purpose. Untick a scope and confirm the write fails. Mint a token for the wrong audience and confirm you reject it. Be Bob, and try to delete Alice's note.

Then ship it.

---

*The full project — three server stages, the local OAuth 2.1 authorization server, all 58 assertions, and configs for Cursor, Claude Desktop, and VS Code — is structured so each stage runs on its own. Clone it, run `verify_browser_flow.py` with `HEADED=1`, and watch Chromium click through the consent screen. It is a more convincing demo than any diagram.*
