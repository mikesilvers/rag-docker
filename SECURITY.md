# Security Policy

## Reporting a vulnerability

Please **do not** report security vulnerabilities in public issues, pull
requests, or discussions.

Report them privately through GitHub instead:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Describe the issue, how to reproduce it, and the impact you expect.

You'll get a reply in the private advisory. Once a fix is ready, we'll publish
the advisory and credit you unless you'd rather stay anonymous.

## Supported versions

Releases are tagged with semantic versions (`v1.0.0` onward). Only the latest tagged
release receives security fixes; see the [latest release](https://github.com/mikesilvers/rag-docker/releases/latest). A reported vulnerability is
fixed privately in its advisory, and the fix is published with a release
alongside the advisory.

## Scope

This platform is built to run locally. Its API requires an account for every
request but `/health` and the login (#25).

- **Accounts and roles.** Each person has their own account with one role
  (End User, Developer, AI Engineer), and the API enforces the role on every
  request. The first AI Engineer is created with the operator command
  `python -m auth` inside the api container, which also recovers lockouts;
  after that AI Engineers manage accounts on the Users page. Accounts are
  disabled, never deleted. There is no self-registration, email recovery, SSO
  or MFA. Passwords are stored as scrypt hashes, generated
  passwords must be changed at first login, and failed logins are throttled.
  See README "Accounts and roles".
- **Sessions.** The browser holds an HttpOnly, `SameSite=Strict` cookie; the
  server keeps only a hash of it, ends it after 12 hours idle or 7 days, and
  ends it at once on logout, disable or password reset. A cookie-authenticated
  change must come from the same origin. The API has no CORS, so a page from
  another origin can't read its answers.
- **Loopback HTTP profile.** The proxy is published on host loopback,
  `127.0.0.1:8080`, so other machines can't reach it. That holds on Docker
  Engine 28.0.0 or newer: Docker documents that older engines still let hosts
  on the same network reach localhost-published ports. Engine 28.0.0 or newer
  is a prerequisite. This assumes Docker's standard bridge/NAT networking;
  custom direct-routing setups are outside this local configuration. Over plain
  HTTP the session cookie is not marked `Secure` (`AUTH_COOKIE_SECURE=false`);
  set it to `true` only when the stack is served over TLS, as LAN mode does.
- **LAN mode (opt-in, #26).** `docker-compose.lan.yml` publishes the proxy on
  every host interface, TLS only (TLS 1.2 and 1.3, Mozilla's intermediate
  ciphers), on port 443 by default, with no plain-HTTP port or redirect; the
  host firewall decides which networks reach it. It marks the session cookie
  `Secure`, and the API refuses to start in LAN mode unless the cookie is
  `Secure` and an enabled AI Engineer exists; then the proxy doesn't start
  either, so nothing is published. The certificate is your own (from
  `proxy/certs/`, ignored by git) or one self-signed on first start for the
  names in `RAG_LAN_HOSTNAMES` plus `localhost`, kept in the `lan_certs`
  volume with its key readable only by its owner; its SHA-256 fingerprint is
  logged so users can check what their browser shows. No HSTS header is sent,
  so a certificate change can't lock browsers out. On Docker Desktop nginx
  doesn't see clients' own addresses (measured: `192.168.65.1` for a request
  through the host's LAN address), so the per-username-and-address login
  throttle works per username there: anyone on the network can lock a username
  for 15 minutes with 5 wrong passwords. See README "Sharing with your office".
- **Service API keys.** Programs authenticate with keys issued by AI Engineers
  to service principals, accounts that can't log in to the browser; keys are
  never issued to people. A key is 256 random bits, shown once at issuance;
  only its SHA-256 is stored. It is accepted only as `Authorization: Bearer`,
  never from a URL, cookie or other header, and is never logged. A key has its
  principal's role at each request, can't manage accounts or keys, expires
  (at most 365 days, 90 by default) and can be revoked; disabling the principal
  stops all its keys. Key requests carry no cookie, so they skip the origin
  check. See README "Service keys".
- **Audit log (#29).** Logins and failed logins, queries, imports, exports,
  deletions, tuning, settings saves, account, principal and key
  administration, the operator command and reads of the log itself are
  appended to `audit/audit.jsonl` in the `ingest_uploads` volume (mode 0600,
  rotated by size, nothing edits or deletes a record), with the server-side
  actor, role, key id, client address and result. It never records a
  password, session cookie, API key, passphrase, answer or document text, and
  records a query's question text only when the operator sets
  `AUDIT_LOG_QUESTIONS=true`. Only AI Engineers' browser sessions can read it;
  API keys can't. A record that can't be written doesn't stop the action; it
  is logged and counted. On Docker Desktop the client address doesn't tell
  clients apart (see LAN mode above). It guards against mistakes and
  after-the-fact questions, not against someone with access to the Docker
  host, who can edit the files. See README "Audit log".
- **Not covered yet.** The MCP server's use of keys is #39. `GET /health` is
  open, for container healthchecks, and reports service status and host
  resources.

Exposing the workbench to an untrusted network is outside the supported
configuration, but reports of issues that make that riskier than expected are
still welcome.
