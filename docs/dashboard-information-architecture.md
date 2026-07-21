# Dashboard information architecture

The dashboard is organized around administrator goals. Raw setting sections and implementation modules are not navigation concepts.

## Primary navigation

| Group | Destinations | Capability rule |
|---|---|---|
| Monitor | Overview, Analytics | `dashboard.view`, `analytics.view` |
| Community | Features, Streaks | `features.view`, `streaks.view` |
| Operations | Bot Operations, Reminders | `operations.view`, `reminders.view` |
| Content | Visual Content Studio, Message Studio, Knowledge, AI | domain-specific view capabilities |
| Finance | Bank | `bank.view` |
| System | Settings, Dashboard Access, Audit Log | `settings.view`, `access.manage`, `audit_log.view` |

Navigation is computed from effective server-side permissions. A missing link is not the security boundary: the same capability policy runs before route handling.

## Settings boundaries

- General (`/settings`): system health and links, not feature fields.
- Dashboard Access (`/settings/access`): users, system/custom roles, Discord mappings, direct assignments, per-user overrides.
- Discord Connection (`/settings/discord`): connection/snapshot status, counts, friendly refresh time, refresh action, errors.
- Data & Storage (`/settings/imports`): import history. Database backup remains in Operations because it is an operation, not a preference.
- Advanced (`/settings/advanced`): allowlisted technical system/import values and compatibility controls.
- Audit Log (`/settings/audit`): authentication, denials, access/configuration changes, sensitive actions.

Integrations are summarized through feature cards and connection health rather than presenting secret provider credentials. Secrets remain `.env`-only and are never rendered.

## Feature configuration flow

1. `/features` filters the registry to capabilities the current user may view.
2. Cards report actual `ENABLED_MODULES` state, configuration completeness, support status, and setting count.
3. `/features/{key}` contains only settings owned by that feature.
4. Discord resources use the live local metadata snapshot and compact selectors.
5. Changes validate together and commit in one SQLite transaction.
6. A dirty-state bar provides Save and Discard; successful writes are shown and audited.
7. Existing operational dashboards remain linked when they add real value. Empty tabs are not created.

## Route compatibility

The following aliases remain intentionally reversible:

- `/stats*` → `/analytics/stats*`
- `/settings/knowledge*` → `/knowledge*`
- `/imports` → `/settings/imports`
- `/users` and `/settings/users` → `/settings/access`
- `/settings/features` → `/features`
- `/settings/permissions` → `/settings/access`

These aliases are not displayed in navigation and can be removed in a later major release after bookmark/deployment evidence is available.

## Responsive and accessibility behavior

- Desktop keeps one fixed primary sidebar; Settings adds one compact secondary navigation only inside Settings.
- At 980px and below the primary navigation is an accessible drawer and Settings navigation becomes a single scrollable row.
- Feature, role, permission, audit, and form grids collapse to one column.
- Tables scroll within their own region rather than widening the page.
- All fields have visible labels and helper text; focus remains visible; skip navigation and semantic headings are retained.
- Deleted/inaccessible Discord resources remain visible as Missing IDs until an administrator explicitly removes them.
