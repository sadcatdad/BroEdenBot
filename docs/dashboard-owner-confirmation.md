# Items requiring owner confirmation

No uncertain item was deleted in this pass.

1. **Remove the orphaned bank log settings?** `BANK_LOG_CHANNEL_ID` and lowercase `bank_log_channel_id` have no current bank-cog consumer. They are hidden and preserved. Confirm deletion only after checking the live production database and any external automation.
2. **Retire VC XP compatibility keys?** `VCXP_MINUTES_PER_PULSE`, `VCXP_ROLE_REMOVE_DELAY_SECONDS`, `VCXP_DAILY_PULSE_CAP`, and `VCXP_WEEKLY_PULSE_CAP` remain stored/validated but hidden. Confirm a removal release after every deployment uses `VC_XP_PULSE_MINUTES` and no rollback target needs the old keys.
3. **Disable legacy reminder commands?** Set `ENABLE_LEGACY_REMINDER_COMMANDS=false` only after staff have transitioned from `/reminder add`, `/reminder manage`, and `/remind subscribe`.
4. **Remove legacy dashboard URL aliases?** Keep them through at least one published release and inspect access logs/bookmarks first.
5. **Choose non-owner Discord mappings.** The schema seeds Moderator, Party Captain, and Analyst/Viewer dashboard roles, but no production Discord role is guessed. An owner must map live Discord roles in Dashboard Access.
6. **Confirm Administrator access to the audit log.** Administrator currently has `audit_log.view` but not `access.manage`. If audit history should be owner-only, create an owner-only policy change before deployment.
7. **Set the re-verification interval.** The default is 60 minutes. Set `DASHBOARD_DISCORD_REVERIFY_MINUTES` between 5 and 1440 based on risk and expected login frequency.
8. **Rate limiting and outer gate.** Keep Cloudflare Access or another trusted outer gate. Application login attempts are audited but this pass does not add a distributed IP rate limiter; confirm whether one is required for the deployment topology.
9. **Production usage classifications.** The checkout lacked production logs. Features marked `UNKNOWN` for recent use need live service/log confirmation before deprecation.
