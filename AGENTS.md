# Dashboard validation

For changes to The Garden dashboard:

- Use `static_asset_url` for bundled CSS, JavaScript, and images. Load page styles and deferred scripts through `page_assets`; preserve shared CSS order.
- Check affected pages and shared navigation in a real browser at desktop and mobile widths. Check key controls, failed requests, JavaScript/CSP console errors, and horizontal overflow.
- Exercise status/form changes through their save and validation paths. Preserve CSRF, live permission checks, ownership, revision history, and private-data boundaries.
- Run appropriate regression tests and `git diff --check`. Update `README.md` for behavior, permission, configuration, or deployment changes.
- After deployment, verify the running release, public asset HTTP/MIME/content hashes through the cache, and bot/dashboard health. Local tests alone do not establish a live fix.
- Use the self-hosted deployment workflow. The bot is no longer hosted on Railway.
