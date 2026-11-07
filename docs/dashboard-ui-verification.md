# Dashboard asset and UI verification

Date: November 7, 2026. This pass checks the dashboard beyond Scout Operations.
Browser checks use isolated sample databases and do not send Discord messages,
publish events, change production permissions, or submit production forms.

## Findings and fixes

| Finding | Fix |
| --- | --- |
| Shared styles/navigation and several page scripts still used fixed or absent version strings | Every template now uses file-content fingerprints for local CSS, JavaScript, and bundled brand images. URLs change automatically with their contents. |
| Event Drops and some editors loaded assets inside the body | Page-specific styles and deferred scripts load through the shared document-head block. |
| BRO Directory search was an inline executable script rejected by the existing security policy | Move it to a versioned external script, preserving the policy. |
| Component `display` styles could reveal elements with the `hidden` attribute | A shared hidden rule preserves filtering and conditional-field behavior. Native `hidden="until-found"` remains supported. |

## Browser coverage

The browser pass visited **83 rendered views** at 1536 × 1000 and 390 × 844.
Every view returned HTTP 200 after normal redirects. No failed asset requests,
JavaScript exceptions, security-policy console errors, or document-level horizontal
overflow were observed. Eighteen distinct local assets loaded successfully using
versioned URLs.

Covered views include:

- Login and the shared desktop/mobile navigation.
- Overview, feature hub, and all 16 feature configuration pages.
- Analytics overview, activity, channels, members, voice, heatmap, exports, and stats.
- Operations, reminders, streaks, bank, and AI usage/knowledge configuration.
- Events list/calendar, event creation, and a seeded event editor.
- Event Drops list, campaign detail/editor, variants manager, and new variant editor.
- Message/Embed Studio library and new embed editor.
- Visual Studio templates, a template editor, assets/upload, themes/new theme,
  globals, and size reference.
- Knowledge, BRO Directory, published profiles, own profile, and staff management.
- Settings, permissions, Discord metadata, Access, users, imports, and audit views.
- Scout overview, six sections, activity/help forms, and seeded activity/report/action details.
- Supported legacy redirects to their current destinations.

BRO Directory filtering was exercised with a matching name and no-match search.
Additional interactive checks cover mobile navigation/Escape, event-type fields,
Event Drop reward/schedule fields and live preview, empty variant rewards,
settings dirty/save state, embed live preview, BROfile color preview, and Knowledge
filtering. These checks change local browser form state without publishing anything.

## Regression coverage and limits

Tests enforce that no template references unversioned static assets or executable
inline scripts blocked by the security policy. Asset tests verify content/MIME,
content-change invalidation, existing files, and proxy-safe paths. Existing route,
permission, private-data, CSRF, storage, form validation, and delivery tests remain
part of the full suite. All **737 tests** passed after these changes, and
`git diff --check` passed.

This confirms the listed rendering and interactive checks; it is not a claim that
every possible configuration, record, upload, or external Discord/API operation has
been exercised. Production verification must follow deployment of this pass and
check the new release, public asset digests, and service health. A previous Scout-only
deployment does not include these additional dashboard-wide fixes.
