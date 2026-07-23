# Visual Content Studio

Visual Content Studio is the authenticated dashboard workspace for the assets,
themes, layout settings, and template configuration used by BroEdenBot's
runtime-generated PNGs. It is intentionally built on the existing Pillow visual
system, FastAPI/Jinja dashboard, shared SQLite database, dashboard roles, CSRF
protection, and Discord output optimizer.

## Concepts and inheritance

- **Templates** are registered image generators with a fixed canvas, supported
  settings, asset slots, validation rules, preview data, and legacy fallback.
- **Assets** are reusable normalized image files. SQLite stores metadata,
  relationships, Discord message references, and durable storage jobs. The
  Discord attachment is the durable source; renderer-ready PNGs and thumbnails
  remain a local cache.
- **Themes** are reusable visual tokens and optional asset references.
- **Global settings** are defaults inherited by all compatible templates.
- **Variants** are optional alternate settings/canvases. Existing commands keep
  using the default unless a renderer explicitly opts into a variant.
- **Schedules** activate a theme (and optionally a variant) for a time range.

Resolution is deterministic:

1. Built-in renderer defaults
2. Published global defaults
3. Selected theme
4. Published template overrides
5. Selected variant
6. Active schedule (template-specific before global, then priority)
7. Explicit runtime overrides supported by the command

## Delivered phases

All planned phases share one registry and resolver rather than separate
feature-specific settings pages:

1. Repository/renderer audit and implementation map.
2. Registry, additive schema, storage abstraction, resolver, and legacy-safe
   runtime bridge.
3. Template cards/editor, deterministic published/draft previews, safe-area
   views, sizing guidance, and draft/publish/reset/history controls.
4. Asset Library upload/replace/rename/archive/restore/delete, normalization,
   thumbnails, duplicate detection, focal cropping, and dependency protection.
5. Reusable themes, global defaults, bundled typography, colors/effects,
   branding slots, and accessibility controls.
6. Per-template layout/content controls plus variants.
7. Scheduled themes, import/export-as-drafts, audit history, and bounded
   version retention.
8. Integration of all 10 discovered PNG generators with existing output-size
   safeguards and safe legacy fallback.
9. Registry, schema, storage, renderer, dashboard, authorization, migration,
   preview, edge-case, and regression tests plus deterministic samples.
10. Environment examples, systemd preflight checks, backed-up/idempotent
    migration, deployment rollback, administrator guide, developer guide, and
    troubleshooting documentation.

If a custom asset or configuration cannot be used, the runtime logs the
template and safe error category, drops the failing custom asset, and continues
through the built-in/legacy renderer path. No absolute paths, SQL, tokens, or
member-private data are returned by dashboard errors.

## Uploading assets

Open **Visual Content Studio → Asset Library → Upload asset**. Selecting a
template and slot updates the guidance before a file is selected. The page then
shows the chosen file's dimensions, ratio, size, type, compatibility, expected
normalization, crop, safe area, and focal point.

Uploads accept still PNG, JPEG, and WEBP files up to 10 MB. The server verifies
the file signature and extension, rejects corrupt/animated/malformed images,
applies EXIF orientation, strips metadata, converts to RGB/RGBA, creates a
renderer-ready normalized PNG and a thumbnail, and stores a SHA-256 checksum.
Original filenames are metadata only. Generated storage keys and root-bound
path resolution prevent traversal.

Wrong-ratio and undersized files require separate explicit acknowledgements.
The upload is normalized only once; bot commands do not reopen an original
full-resolution upload. Focal points are expressed from 0 to 1 on each axis.

Before uploading, set **Asset Library Storage Forum Post** under **Features →
Visual Content Studio** to an existing private Discord forum-post/thread ID.
The dashboard makes no Discord API call. It saves the normalized local copy and
queues an idempotent job; the live bot posts one attachment message inside that
thread, records the message and attachment URL, and the Asset Library switches
its display to that URL. A local normalized file is retained as the fast
renderer cache. If that cache is missing, only allowlisted
`cdn.discordapp.com` or `media.discordapp.net` attachment URLs may rebuild it.

Existing active assets are backfilled when the `visual` module starts.
Changing the configured storage thread queues the current active library into
the new thread. Each old storage message is removed only after the replacement
message and URL have been recorded. Replacing an asset edits its existing
storage message when possible.

The current, registry-generated dimension table is in
[`visual-content-studio-size-reference.md`](visual-content-studio-size-reference.md).
Regenerate it after a registry change:

```bash
.venv/bin/python scripts/generate_visual_size_reference.py
```

## Editing and publishing

Open a template and compare its published and draft previews. The preview uses
deterministic public sample data, including long Unicode names, large values,
maximum rows, missing avatars, and empty states. It never reads staff/private
member data. The safe-area preview marks the actual header, content panel,
footer, and queue-avatar/name regions.

1. Choose a theme and assets, then edit only the controls supported by that
   template.
2. Save a draft. Live bot output is unchanged.
3. Check maximum-row, empty-state, full-resolution, and safe-area previews.
4. Publish. Publishing validates settings and assets and generates a maximum-
   row test image before updating SQLite atomically.

The last 20 published versions are retained. Restore creates a new draft; it
does not replace live output until explicitly published. Reset likewise loads
built-in defaults into a draft.

Global visual settings also use a save-draft/publish flow. Theme edits take
effect only for configurations that currently resolve to that theme. Asset
archive/delete protection uses the normalized usage table and shows the exact
template/theme/variant dependencies on the asset detail page.

## Themes, variants, and schedules

The built-in **Bro Eden Default** theme cannot be deleted and matches the
existing production palette and bundled fonts. Custom themes may be created,
duplicated through the editor/export workflow, set as default, archived, and
permanently deleted only when unused.

Only the bundled and tested font families are accepted: Open Sans Emoji,
Calibri Regular, and Calibri. Arbitrary font upload is intentionally disabled.

Schedules use ISO date-times plus the configured server timezone. Overlap is
resolved by template scope before global scope, then descending priority, then
newest schedule. When a schedule ends, normal inheritance resumes automatically.

## Import, export, and backup

`GET /visual/export` produces a versioned
`broeden.visual-content-studio` JSON document with registry keys, globals,
themes, and template configuration. Import validates the schema and supported
template keys, adds only non-conflicting theme names, and stages template/global
configuration as drafts. Nothing imported becomes live automatically.

Authoritative backup data is:

- the shared SQLite database (`DATABASE_PATH`);
- the private Discord storage forum post containing the source attachments;
- `VISUAL_ASSET_DIR/normalized` and `VISUAL_ASSET_DIR/thumbnails`;
- the environment/configuration that points both services to the same directory.

Previews and caches are reproducible. The deployment script backs up SQLite and
the asset directory before migrations. A manual backup can be made with:

```bash
mkdir -p backups/visual-content-studio
sqlite3 data.db ".backup 'backups/visual-content-studio/data.sqlite'"
tar -czf backups/visual-content-studio/assets.tar.gz data/visual-assets
sqlite3 backups/visual-content-studio/data.sqlite "PRAGMA quick_check;"
```

## Adding a new PNG generator

Keep queries, calculations, sorting, and permission checks outside the visual
layer. Then:

1. Define a built-in renderer that works without dashboard configuration.
2. Add a `TemplateDefinition` to `utils/visual_studio/registry.py`.
3. Use a stable snake-case key, accurate canvas, output target, category,
   command source, and maximum item count.
4. Declare only settings the renderer actually accepts.
5. Declare every asset destination as an `AssetSlot` with exact recommended,
   minimum, maximum, ratio, alpha, fit, and safe-area geometry.
6. Resolve configuration once through `load_runtime_customization()` (async) or
   `load_runtime_customization_sync()` when already running in a worker thread.
7. Pass the resolved style/assets to the centralized Pillow canvas and preserve
   the old arguments/assets as legacy fallbacks.
8. Add deterministic preview coverage, invalid/missing asset tests, default and
   custom-theme visual tests, output-limit tests, and dashboard guidance checks.
9. Regenerate the Markdown size reference and update the implementation map.

Minimal registration example:

```python
TemplateDefinition(
    key="event_winners",
    display_name="Event Winners",
    description="Highlights event placements.",
    category="events",
    renderer="cogs.events.render_winners",
    width=1600,
    height=900,
    supported_settings=("background", "theme", "accent_color", "title"),
    asset_slots=(
        AssetSlot(
            key="background",
            label="Full-canvas background",
            asset_type="background",
            recommended_width=1600,
            recommended_height=900,
            minimum_width=1280,
            minimum_height=720,
            maximum_width=4800,
            maximum_height=2700,
            safe_area=SafeArea(80, 80, 80, 80),
        ),
    ),
    defaults={"accent_color": "#f0319b"},
    command_source="/event winners",
)
```

Duplicate keys and duplicate slot names fail during registry construction.

## Migration and deployment

Initialize against a copy first:

```bash
cp data.db /tmp/broeden-visual-migration.db
.venv/bin/python scripts/migrate_visual_content_studio.py \
  --database /tmp/broeden-visual-migration.db \
  --asset-dir /tmp/broeden-visual-assets \
  --backup-dir /tmp/broeden-visual-backups
.venv/bin/python scripts/migrate_visual_content_studio.py \
  --database /tmp/broeden-visual-migration.db \
  --asset-dir /tmp/broeden-visual-assets \
  --validate-only
```

Production uses the normal `./deploy.sh` workflow. Before restarting, confirm:

- the dashboard service user can write `VISUAL_ASSET_DIR`;
- the bot service can read it;
- Pillow and bundled fonts load in the production venv;
- disk space covers the SQLite and asset backups;
- both services point to the same `DATABASE_PATH` and `VISUAL_ASSET_DIR`.
- `visual` is present in `ENABLED_MODULES`;
- `VISUAL_ASSET_STORAGE_THREAD_ID` names an existing private forum post/thread;
- the bot can view the post, read its history, send messages and attachments in
  it, and reopen it when archived.

The migration is additive and idempotent. Roll back code by fast-forwarding or
reverting the deployment commit, restoring the pre-deploy SQLite snapshot and
asset archive, then restarting both services. Legacy background files and BLOB
banners remain intact, so a code rollback returns renderers to the old path.
Do not drop Studio tables during an ordinary rollback.

## Environment variables

- `VISUAL_ASSET_DIR`: shared persistent storage. Default:
  `data/visual-assets`; production recommendation:
  `/home/sadcatdad/BroEdenBot/data/visual-assets`.
- `VISUAL_ASSET_STORAGE_THREAD_ID`: one existing private Discord forum-post or
  thread ID. It is editable under **Features → Visual Content Studio**. Do not
  use a forum channel ID; paste the post/thread ID itself.
- `VISUAL_RENDER_CONCURRENCY`: concurrent centralized ranked/roster renders,
  bounded from 1 to 4; default `2` for Raspberry Pi stability.
- `STATS_IMAGE_TARGET_BYTES`: existing per-PNG Discord target, default 8 MB.

## Known limits

- The current permission model is broad: authenticated users may view; only
  dashboard `owner` and `admin` accounts may mutate or publish. The schema and
  routes are ready for future granular `visual_content.*` permissions.
- Variant selection is dashboard/schedule-driven. Discord commands intentionally
  keep their existing interface.
- JSON exports contain configuration metadata, not binary assets. Asset ZIP
  packages can be added later; preserve both the SQLite references and Discord
  storage post, with the documented filesystem backup as a recovery cache.
