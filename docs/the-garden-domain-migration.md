# The Garden public-domain migration

This runbook moves the authenticated member platform from
`dashboard.broeden.com` to `garden.broeden.com` without changing databases,
Discord role mappings, permissions, or Railway volumes.

## Application configuration

Set these variables on the web service:

```dotenv
DASHBOARD_PUBLIC_URL=https://garden.broeden.com
DASHBOARD_LEGACY_HOSTS=dashboard.broeden.com
DISCORD_OAUTH_REDIRECT_URI=https://garden.broeden.com/auth/discord/callback
DASHBOARD_COOKIE_SECURE=true
```

Keep the existing `DASHBOARD_SECRET_KEY`. Changing it would invalidate every
signed session. The session cookie is host-only, HTTP-only, SameSite Lax, and
limited to 12 hours. Existing sessions on `dashboard.broeden.com` therefore do
not transfer to `garden.broeden.com`; members sign in once on the new hostname.
Do not set a parent-domain cookie solely to avoid that one-time login.

The app has no cross-origin browser API and does not require CORS. It does not
currently enforce an allowed-host list. Railway's internal health hostname can
therefore continue to reach `/health`, while only explicitly listed legacy
hosts redirect.

The Railway startup command trusts the platform edge's forwarded scheme and
host headers. This keeps login and permission redirects on `https://` behind
Cloudflare and Railway. Do not copy the Railway-only
`--forwarded-allow-ips="*"` setting to a directly exposed local deployment.

## Discord Developer Portal

1. Open the existing BroEdenBot application and choose **OAuth2**.
2. Add the exact redirect
   `https://garden.broeden.com/auth/discord/callback`.
3. Keep `https://dashboard.broeden.com/auth/discord/callback` temporarily.
4. Save, then set the web-service `DISCORD_OAUTH_REDIRECT_URI` to the new value.
5. Complete a new Discord login at `garden.broeden.com`.
6. Remove the old redirect after the cutover window and access-log review.

The authorization request and token exchange must use the same exact redirect
URI. Do not rely on the old hostname redirect for an OAuth callback already in
flight; start the login again on The Garden.

## Cloudflare and Railway routing

First identify which pattern currently serves `dashboard.broeden.com`.

### If Railway owns the public origin

1. In the existing Railway `broedenbot` service, confirm the source branch is
   `main` and the latest deployment contains this migration.
2. Add `garden.broeden.com` as a custom domain on that same service. Do not
   create a second service or volume.
3. In Cloudflare DNS, add the CNAME target Railway displays. Use the proxy mode
   required by the existing working domain and allow Railway's certificate to
   become ready.
4. Verify `https://garden.broeden.com/health` returns HTTP 200.

### If Cloudflare Tunnel owns the public origin

1. Open the existing tunnel and copy the current
   `dashboard.broeden.com` public-hostname service target.
2. Add `garden.broeden.com` to that same tunnel and same service target.
3. Let Cloudflare create or update the proxied tunnel DNS record.
4. Verify the tunnel connector and origin are healthy, then verify
   `https://garden.broeden.com/health` returns HTTP 200.

Do not configure both patterns for the same hostname. A Cloudflare 502 means
DNS reached Cloudflare but its configured origin or tunnel did not return a
valid response; fix that origin path before enabling the redirect.

## Legacy redirect

After The Garden health check and OAuth login both pass, create a Cloudflare
Single Redirect:

- Incoming hostname equals `dashboard.broeden.com`.
- Target hostname is `garden.broeden.com`.
- Preserve the path and query string.
- Use a permanent redirect (301 or 308).

The application also returns 308 for hosts in `DASHBOARD_LEGACY_HOSTS`, but the
Cloudflare rule is preferred because it prevents the old hostname from starting
an application session.

## Verification

1. `https://garden.broeden.com/health` returns 200.
2. `https://garden.broeden.com/login` shows **The Garden**.
3. Discord login returns to
   `https://garden.broeden.com/auth/discord/callback` and succeeds.
4. A Verified-only member sees **Events** and no unauthorized admin routes.
5. An owner sees the **Admin Dashboard** navigation and existing settings.
6. `https://dashboard.broeden.com/events?month=2026-07` redirects to the same
   path and query on `garden.broeden.com`.
7. Response cookies on the new hostname include `Secure`, `HttpOnly`, and
   `SameSite=Lax`.
8. An anonymous request to `/events` redirects to an
   `https://garden.broeden.com/login` URL, never `http://`.
9. Railway health, logs, database mounts, and replica count are unchanged.

## Rollback

Disable the Cloudflare redirect, restore the old Railway/tunnel custom hostname,
set `DISCORD_OAUTH_REDIRECT_URI` back to the old callback, and redeploy. Keep the
same databases, volume, signing key, and OAuth client secret. The code change is
schema-free and requires no database rollback.
