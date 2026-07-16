# Visual Content Studio implementation map

This document records the source audit performed before the Visual Content
Studio implementation. The registry in `utils/visual_studio/registry.py` is the
runtime source of truth; this file explains the migration decisions and
deployment constraints.

## Current architecture

- The bot is discord.py with an aiosqlite connection to the shared `data.db`.
- The dashboard is a lightweight, server-rendered FastAPI/Jinja application.
- Dashboard users have `owner`, `admin`, or `viewer` roles. Existing CSRF and
  write-access helpers protect mutations; the Studio follows the same model.
- Generated statistics graphics are centralized in the Pillow-based
  `utils/stats_visuals/` package. Render results already paginate and enforce
  `STATS_IMAGE_TARGET_BYTES` (8,000,000 bytes by default).
- Existing custom leaderboard and role-roster banners are stored as BLOBs in
  legacy tables. They remain readable during migration and take effect when no
  Studio asset overrides the corresponding slot.
- Production runs from `/home/sadcatdad/BroEdenBot` under the `sadcatdad`
  service user. Local development runs from the checkout. The Studio therefore
  uses a configurable persistent asset directory rather than deployment-owned
  static files.
- There is no general migration runner. Existing features use idempotent schema
  initializers and additive `ALTER TABLE` operations. The Studio follows that
  convention and also ships a dedicated migration/validation CLI.
- The dashboard already accepts form data but had no reusable binary upload
  service. Studio uploads use Pillow content detection, generated storage keys,
  normalized derivatives, thumbnails, checksums, and bounded dimensions.
- There was no generated-image cache. Studio runtime configuration and decoded
  assets use bounded caches that are invalidated by writes.

## Generated PNG inventory

| Template key | Display name | Command/feature | Renderer | Canvas | Ratio | Background/banner source | Maximum items and upload behavior | Migration |
|---|---|---|---|---:|---:|---|---|---|
| `custom_leaderboard` | Custom Leaderboard | `/leaderboard`, buttons | `utils/stats_visuals/renderers.py` via `utils/ranked_graphic.py` | 1200x1500 | 4:5 | Legacy banner BLOB; no background | 10 rows per command page; one optimized PNG | Medium |
| `bump_leaderboard` | Bump Legends Leaderboard | `!bumpscores`, managed bump post | same ranked renderer | 1200x1500 | 4:5 | `assets/bump_leaderboard_background[.png]` (legacy source is 1200x1240) | 10 rows; one optimized PNG | Low |
| `streak_leaderboard` | Activity Streak Leaderboard | `/streak leaderboard`, weekly post | same ranked renderer | 1200x1500 | 4:5 | `assets/streak_leaderboard_background.png` (legacy source is 1200x1240) | 10 rows; one optimized PNG | Low |
| `activity_leaderboard` | Activity Leaderboard | `/stats activity` and tracked activity reports | `render_ranked_graphic_result` | 1200x1500 | 4:5 | None | 10 rows per image; multi-attachment pagination | Low |
| `vc_leaderboard` | Voice Leaderboard | `/vcstats leaderboard` | `render_ranked_graphic_result` | 1200x1500 | 4:5 | None | 10 rows per image; multi-attachment pagination | Low |
| `role_roster` | Role Roster | tracked role graphics | `render_compact_roster_result` | 1200x1500 | 4:5 | Legacy 1104x208 banner BLOB | 12 rows per image; multi-attachment pagination | Medium |
| `role_comparison` | Role Comparison | `/stats rolecompare`, tracked report | `render_rolecompare_result` | 1600x900 | 16:9 | None | Five metrics plus comparison diagram; one PNG | Low |
| `missing_role` | Missing Role Coverage | `/stats missingrole`, tracked report | `render_missingrole_result` | 1600x900 | 16:9 | None | Three metrics plus coverage bar; one PNG | Low |
| `stats_error` | Stats Error Card | tracked stats fallback | `render_error_result` | 1600x900 | 16:9 | None | Bounded error text; one PNG | Low |
| `queue_next` | Queue Up Next Banner | `/queue dashboard` and controls | `cogs/queue.py:create_banner` | 1024x258 | 512:129 | `assets/up_next.png`, avatar at 667,45 (167x167) | One member; one optimized PNG | Medium |

Bundled `assets/votenow.png`, `assets/results.png`, and `bar.gif` are static
Discord attachments rather than runtime-generated PNGs. They are not renderer
templates. They remain preserved and may be imported into the Asset Library.

## Shared visual assets and geometry

- Fonts: `assets/OpenSansEmoji.ttf` primary, with
  `assets/calibri-regular.ttf` and `assets/calibri.ttf` fallbacks.
- Dashboard/graphics palette: canvas `#0c0d12`, card `#171820`, accent
  `#f0319b`, plus the tokens in `utils/stats_visuals/theme.py`.
- Ranked and roster content: 48px outer margin; header rectangle
  1104x208 at (48,48); main panel begins at y=286; footer occupies the bottom
  48px region. These values define the registry safe-area overlays.
- Wide report content: 48px outer margin, 1600x900 canvas, header at the top,
  footer at the bottom.
- Queue content: username begins at (120,150); avatar occupies
  (667,45)-(834,212). The base-image safe-area overlay exposes both regions.

## Proposed architecture and migration order

1. Add an immutable template registry containing canvas, settings, slot
   geometry, upload guidance, defaults, and preview factories.
2. Add idempotent Studio tables, a filesystem asset store, shared configuration
   resolution, a bounded cache, import/export, variants, schedules, audit
   records, and a publish/version workflow.
3. Add authenticated dashboard pages and APIs. All writes remain admin/owner
   only under the existing broad role model.
4. Integrate the shared ranked renderer first, then the compact roster and wide
   reports, and finally the queue banner. Legacy arguments/assets stay as the
   fallback beneath a valid published Studio override.
5. Keep calculations and command interfaces unchanged. Only presentation
   settings are resolved through the Studio.

## Database changes

The additive schema adds `visual_assets`, `visual_themes`, `visual_templates`,
`visual_template_versions`, `visual_template_variants`, `visual_schedules`,
`visual_asset_usage`, `visual_global_settings`, and `visual_audit_log`. Foreign
keys prevent deletion of referenced records. JSON is restricted to renderer-
specific settings and portable documents.

## Dashboard and API changes

The top-level Content navigation gains Visual Content Studio. Server-rendered
pages cover Templates, Assets, Themes, Global Settings, template editing,
history, variants/schedules, upload-size reference, and import/export. JSON
endpoints provide registry metadata, compatibility inspection, and authenticated
preview images without exposing filesystem paths or private member data.

## Testing strategy

- Unit tests cover registry validation, schema, resolution precedence, assets,
  MIME spoofing, path safety, cropping, drafts/publishing/restore, schedules,
  imports, fallback, and output limits.
- Dashboard tests cover authentication, authorization, navigation, upload
  guidance, mutations, previews, and mobile-accessible markup.
- Existing renderer tests remain the regression baseline. Deterministic preview
  samples add default/custom/edge-case coverage.
- The full unittest suite, compileall, `pip check`, SQLite quick checks, and
  generated sample inspection are required before deployment.

## Deployment considerations

- `VISUAL_ASSET_DIR` defaults to `data/visual-assets`; production should set it
  explicitly to a persistent path writable by the dashboard and readable by the
  bot, normally `/home/sadcatdad/BroEdenBot/data/visual-assets`.
- Back up both `data.db` and the asset directory. Preview/cache data is
  reproducible and does not need authoritative backup treatment.
- Both services require restart after code/schema deployment. The migration CLI
  is idempotent and supports validation against a copied database.
- Legacy bundled backgrounds and BLOB banners remain available until production
  verification is complete.
