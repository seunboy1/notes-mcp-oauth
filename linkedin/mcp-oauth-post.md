"And of course, in production you'd add authentication."

That line ends most MCP tutorials. It's a bit like a flight manual that ends with "and of course, in production you'd add landing."

So I built the same MCP server three times and proved each stage with 58 assertions that run against the real servers over the real protocol. No mocks.

𝗦𝘁𝗮𝗴𝗲 𝟭 — 𝗹𝗼𝗰𝗮𝗹 (𝘀𝘁𝗱𝗶𝗼)
The client spawns your server as a subprocess. No port, no network, no credentials. The OS process boundary *is* the security model. Genuinely fine for personal tools.

𝗦𝘁𝗮𝗴𝗲 𝟮 — 𝗿𝗲𝗺𝗼𝘁𝗲 (𝗛𝗧𝗧𝗣)
Now it has a URL. My suite connects with no credentials at all and deletes data. That isn't a bug I introduced — it's what a URL without auth means. And URLs leak: screenshots, shell history, config files, support tickets.

𝗦𝘁𝗮𝗴𝗲 𝟯 — 𝗿𝗲𝗺𝗼𝘁𝗲 + 𝗢𝗔𝘂𝘁𝗵 𝟮.𝟭
A real resource server: RFC 9728 metadata, a 401 that tells the client where to log in, every bearer token verified against the authorization server's JWKS (signature, issuer, audience, expiry), a scope enforced per tool, and every query filtered by the `sub` claim.

Four things I'd call non-negotiable:

𝗔𝘂𝗱𝗶𝗲𝗻𝗰𝗲 𝘃𝗮𝗹𝗶𝗱𝗮𝘁𝗶𝗼𝗻 𝗶𝘀𝗻'𝘁 𝗼𝗽𝘁𝗶𝗼𝗻𝗮𝗹. If you don't check that a token was minted for *your* resource, any server your user logs into can replay their token against you. That's the confused-deputy problem the MCP spec calls out by name.

𝗛𝗶𝗱𝗶𝗻𝗴 𝗮 𝘁𝗼𝗼𝗹 𝗶𝘀 𝗻𝗼𝘁 𝗮 𝗰𝗼𝗻𝘁𝗿𝗼𝗹. Leaving a tool out of `tools/list` is a UX affordance — a client can still call it by name. Scope checks belong in the tool body too.

𝗡𝗲𝘃𝗲𝗿 𝗸𝗲𝘆 𝗱𝗮𝘁𝗮 𝗼𝗻 𝗲𝗺𝗮𝗶𝗹. Users change it, and some providers hand it over unverified. Use `sub`.

𝗗𝗼𝗻'𝘁 𝗹𝗲𝗮𝗸 𝗲𝘅𝗶𝘀𝘁𝗲𝗻𝗰𝗲. `get_note` returns the same "not found" whether the id is absent or owned by someone else.

The part that took longest: you cannot test an auth flow you cannot run. So the repo also ships a complete OAuth 2.1 authorization server for localhost — discovery, dynamic client registration, PKCE, JWKS, a consent screen, refresh-token rotation. ~450 lines, dev only.

That's what makes the headline test possible: Playwright drives the whole dance in Chromium — anonymous call → 401 → discovery → dynamic registration → PKCE → consent → loopback redirect → code exchange → tool calls → refresh rotation.

It also asserts the failure modes, which is the actual point. Authorize without PKCE is rejected. A replayed code is rejected. A foreign-audience token is rejected. And a user who unticks `notes:write` on the consent screen gets a token that genuinely cannot write.

Fittingly, while writing this post my own suite "failed" 7 checks. Cause: tokens minted for `127.0.0.1` hitting a server whose identity is `localhost`. Audience validation doing its job perfectly. I fixed the default and kept the lesson.

All three stages and all 58 checks are here:
https://github.com/seunboy1/notes-mcp-oauth

Is shipping a remote MCP server without auth ever acceptable?

#MCP #OAuth #AIAgents #ModelContextProtocol #AppSec
