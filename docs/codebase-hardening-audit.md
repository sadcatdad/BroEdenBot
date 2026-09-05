# Codebase hardening audit

Date: September 4, 2026 (America/Chicago).

## Scope and approach

Repository-wide inventory and static scanning covered the bot entry point,
Discord cogs, shared services, dashboard routes/templates/scripts, import and
migration tools, deployment scripts, dependencies, and tests. Manual review
focused on authentication, capability checks, CSRF, private/public knowledge
boundaries, dynamic SQL, exports, uploads/downloads, image decoding, database
lifetimes, fixed operational commands, and dashboard interaction behavior.

The starting checkout was clean. Existing tests first passed unchanged: 519
passed. Fixes were validated in an isolated Python 3.12.14 environment with the
updated requirements. The existing Python 3.9 `.venv` was not replaced. Browser
work used a separate localhost dashboard and synthetic data in `/private/tmp`.
No deployment, live Discord writes, or production configuration changes were
performed.

## Improvements implemented

| Area | Finding and change |
| --- | --- |
| Dependencies | The original pinned requirements had 46 published advisories across eight packages. Updated those packages, FastAPI, and explicitly pinned a patched Starlette. The resolved 47-package runtime scan has zero known advisories at audit time. |
| Password login | Added an atomic SQLite counter shared across workers: five attempts per client address in five minutes, HTTP 429 and `Retry-After`, expiry and successful-login reset, and a bounded counter table. Password verification runs outside the event loop. Unknown accounts perform equivalent password work; stored hash parameters and input size are bounded. |
| Role downgrades | Historical legacy Owner assignments could override a user's current lower role. Permission resolution now uses the current legacy role, while retaining intentional direct/mapped assignments. Displayed role names follow the same rule. |
| Malformed authentication input | Non-ASCII CSRF/OAuth state values now fail validation instead of raising string-comparison errors. Invalid OAuth token payloads and malformed guild-member structures fail closed. |
| Request and browser boundaries | Added limits on actual streamed bytes as well as declared length, explicit no-store responses for dynamic pages/downloads, anti-framing and MIME-sniffing protections, no-referrer policy, and a CSP that permits only app-hosted JavaScript. Moved inline handlers and scripts to external files. |
| Images | Bounded Asset Library reads, streamed avatar downloads, restricted Discord media recovery to the fixed HTTPS CDN hosts without redirects, capped avatar cache bytes, and checked image dimensions before decoding. Oversized/decompression-bomb images produce validation errors or safe avatar fallbacks. |
| CSV exports | Shared safe writers neutralize formula-like text across analytics, tracked stats, Discord stats/voice reports, checklists, and Event Drops. Numeric values and normal CSV quoting remain intact. |
| Logs and backups | Dashboard and Discord log views share redaction of prefixed credential names and quoted values. All recovery archives under `backups/`, including deployment asset archives, are ignored by Git. |
| SQLite | User, RBAC, and event connections close deterministically after commit or rollback. |
| Dashboard consistency | Editor/action controls use the relevant feature capability. Knowledge readers no longer receive unusable mutation controls. Corrected the VC XP settings destination, made the overview AI summary a compact full-width strip, and improved mobile drawer visibility, focus trapping, and Escape focus restoration. |
| Maintenance | Removed unused imports and wildcard event re-exports, replaced dynamic asyncio imports, handled invalid knowledge sync limits, and fixed extension-loading test isolation exposed by Python 3.12. |

## Verification

- Full pytest suite: **539 passed**, with 19 additional subtests passing.
- Documented `unittest discover` command: **539 tests passed**.
- Twenty new regression tests cover login throttling/atomicity, malformed input,
  request limits, response headers, stale role downgrades, delegated editors,
  database closure, CSV/redaction behavior, and image/network limits.
- Resolved runtime dependency audit: **47 packages, zero known vulnerabilities**.
- `pip check`: no broken requirements.
- Ruff fatal/error and undefined-name checks passed across source and tests;
  the complete F-rule check passed across production Python code.
- Python compilation, all 11 dashboard JavaScript syntax checks, deployment
  shell syntax, and `git diff --check` passed.
- Repository safety check: no tracked sensitive files or token-shaped lines;
  expected feature files and settings definitions present.
- Bandit scanned production Python with no parse errors or high-severity
  findings. Its remaining heuristic warnings include dynamic SQL construction,
  fixed subprocess invocations, intentional service bind addresses, and
  non-cryptographic randomness. These warnings are not a proof of exploitation;
  parameterized query values and allowlisted identifiers remain essential.
- Browser checks at desktop and 390px phone width covered overview layout,
  mobile menu focus cycling and Escape, Knowledge filtering, Message Studio
  live Markdown preview and saving a fixture asset. No script/CSP errors were
  observed in the tested flows.

## Deployment considerations

The new requirements need Python 3.10 or newer; use Python 3.12 or 3.13 and a
fresh virtual environment when upgrading from Python 3.9. The existing Docker
base already uses Python 3.13. FastAPI/Starlette and Pillow receive substantial
version updates, so retain the normal database/assets backups and rollout checks.

The in-app login throttle trusts only the client address resolved by the ASGI
server. Configure the actual proxy trust boundary correctly. Shared proxy/NAT
addresses share a limit, and internet-facing installations should also use an
edge rate limit. Railway's existing wildcard forwarded-header trust assumes
its app port is reachable only through the trusted ingress; verify that
assumption before exposing any direct origin access.

TLS, secure cookies, reverse-proxy behavior, systemd privileges, live Discord
OAuth membership/role changes, and Discord delivery need verification in the
actual deployment. Existing tests exercise these integration boundaries through
fixtures/mocks. The suite still reports upstream deprecation warnings from
Google's SDK, Starlette's HTTPX TestClient adapter, and Discord's audioop import.
These are separate from the successful runtime compatibility and advisory checks.

This audit improves the reviewed code and records its verification limits. A
passing test suite or advisory scan does not establish that every possible
vulnerability has been eliminated.

## Advisory references

The dependency decisions used PyPI package metadata and pip-audit's advisory
results. Particularly relevant request-parser fixes are documented in the
[Starlette form-limit advisory](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq)
and the [python-multipart header-limit advisory](https://github.com/Kludex/python-multipart/security/advisories/GHSA-pp6c-gr5w-3c5g).
The [aiohttp security advisories](https://github.com/aio-libs/aiohttp/security/advisories)
cover the HTTP client/server fixes included in the dependency update.
