# Bro Eden Stats Visual System

All public statistics PNGs use `utils/stats_visuals/`. Commands and services
prepare display data; the visual package lays it out, fetches avatars, renders
the image, and validates the final bytes. Database queries and ranking rules do
not belong in rendering components.

## Architecture

- `theme.py` is the source of truth for brand colors, typography sizes,
  spacing, minimum text size, row limits, and layout profiles. Its palette is
  derived from `dashboard/static/styles.css`.
- `models.py` defines `RenderResult`, ordered `RenderPage` values, warnings,
  and internal diagnostics.
- `components.py` contains the base canvas, header, section heading, metric
  card, trend indicator, chart container, legend, rank badge, avatar container,
  leaderboard row, empty/error states, footer, date-range label, page indicator,
  and Bro Eden mark.
- `text.py` owns number/date/percentage formatting, pluralization, font
  fallback, glyph fallback, wrapping, measurement, and bounded truncation.
- `avatars.py` owns the shared 500-item memory cache, per-render reuse, bounded
  concurrent downloads, timeouts, first-frame handling for animation, EXIF
  orientation, center cropping, circular masks, and fallback behavior.
- `output.py` performs PNG encoding, byte measurement, fallback optimization,
  minimum-dimension enforcement, and render diagnostics.
- `renderers.py` contains data-only view models and the current leaderboard,
  roster, role-comparison, missing-role, and error renderers.
- `utils/ranked_graphic.py`, `utils/compact_roster.py`, and
  `utils/stats_reports.py` are compatibility adapters. New code should import
  the structured render functions from `utils.stats_visuals`.

No browser renderer or chart framework is used. Pillow remains the only image
stack. Current heatmaps and trend views are Discord embeds or dashboard HTML,
not generated PNGs; a future static chart should use the shared chart
container and theme tokens rather than introducing its own palette.

## Layout profiles and density limits

| Profile | Baseline | Minimum | Intended use |
| --- | --- | --- | --- |
| `wide_overview` | 1600 x 900 | 1280 x 720 | Summary metrics and role reports; at most six metrics and two major charts |
| `portrait_leaderboard` | 1200 x 1500 | 960 x 1200 | Mobile-readable rankings and rosters |
| `square_summary` | 1200 x 1200 | 960 x 960 | Compact future summaries that genuinely fit a square |

Leaderboards use 10 rows per PNG. Role rosters use 12 rows per PNG. Additional
items create additional pages; body text is never shrunk below the central
minimum to keep a page count low. Every page repeats its title, scope, section,
page indicator, and update time. Usernames are individually truncated with an
ellipsis, so one long name never reduces the typography for every member.

Pagination happens before rendering. Existing ranking order and supplied
`rank_start` values are preserved, including the caller's tie behavior.
`RenderResult.attachments("name.png")` produces deterministic names in upload
order: `name.png` for one page or `name_1.png`, `name_2.png`, and so on.

## Requesting a render

Use data-only view models and retain the structured result:

```python
result = await render_ranked_graphic_result(
    title="Top Text Participants",
    subtitle="Last 30 days",
    sections=[RankedGraphicSection("Members", items)],
    updated_at=now,
    accent_color=COLOR,
)

files = [
    discord.File(io.BytesIO(data), filename=filename)
    for filename, data in result.attachments("text_activity.png")
]
await interaction.followup.send(files=files)
```

Do not query SQLite, fetch Discord members, or calculate scores inside a
renderer. Prepare exact and abbreviated display strings before rendering when
the command has domain-specific formatting needs. Shared generic formatting
lives in `text.py`.

## PNG file-size protection

`STATS_IMAGE_TARGET_BYTES` sets the maximum accepted bytes for each generated
PNG and defaults to `8000000` (8 MB). Each page is rendered independently and:

1. converted to RGB when transparency is unnecessary;
2. written as an optimized, metadata-free PNG at compression level 9;
3. measured from the actual upload bytes;
4. carefully palette-quantized only when the lossless result is too large;
5. re-rendered at successively smaller dimensions, never below the profile's
   approved minimum, if it remains over target; and
6. rejected with `ImageSizeLimitError` if no readable output can satisfy the
   configured target.

Content renderers paginate before this pipeline, so oversized member lists are
split rather than compressed into tiny rows. The renderer never returns a page
that exceeds the configured target. Optimization steps are logged.

Successful renders log graphic type, profile, dimensions, page count, render
duration, final page byte sizes, optimization status, text truncation count,
avatar fallback count, and overflow warnings. These diagnostics remain in bot
logs and are not shown to ordinary members.

## Adding or changing a graphic

1. Pick one of the existing profiles from `theme.py`. Add a new profile only
   when the content cannot reasonably use an existing one.
2. Define immutable, data-only view models in `renderers.py`.
3. Compose helpers from `components.py`; do not copy pixel, font, palette,
   avatar, or output code into a cog.
4. Decide content limits and paginate the view model before creating render
   factories.
5. Send all `RenderResult.attachments()` in order and provide a concise member
   error while logging the internal exception if rendering fails.
6. Add deterministic fixtures for empty, boundary, overflow, Unicode, avatar,
   and large-volume states.
7. Generate and visually inspect the review set at full resolution and at a
   phone-sized preview.

Change brand tokens in `theme.py` together and compare them against the live
dashboard CSS. Preserve high contrast and minimum typography. Pride accents
should remain selective; chart categories must still be distinguishable
without relying on a full rainbow or subtle color differences alone.

## Tests and visual review

Run the renderer tests:

```bash
PYTHONPYCACHEPREFIX=/tmp/broeden-pyc \
  .venv/bin/python -m unittest tests.test_stats_visuals -v
```

Generate the ignored visual review set:

```bash
PYTHONPYCACHEPREFIX=/tmp/broeden-pyc \
  .venv/bin/python scripts/generate_stats_visual_samples.py
```

The default output is `dev-output/stats-visuals/`, which is ignored by Git.
It includes overview, text, VC, role, leaderboard first/later pages, empty,
long-name, and 100-member stress examples. Inspect the PNGs for clipping,
overlap, contrast, alignment, avatar crop, hierarchy, whitespace, and file
size before merging a theme or layout change.

## Remaining non-stats image code

`cogs/queue.py` generates an unrelated karaoke/voice queue banner and retains
its established asset-specific layout. Poll and startup images are static
assets. They are not statistics graphics and were intentionally left outside
this phase.

