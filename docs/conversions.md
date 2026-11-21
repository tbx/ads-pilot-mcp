# Conversion actions: what ads-pilot can change

Conversion actions decide what smart bidding optimizes for. That is why the write tools need the access level `full` and check more than other tools.

## Tools

| Tool | What it does |
|---|---|
| `list_conversion_actions` | Every action with type, category, origin, status, primary or secondary, counting, value, windows, call length, GA4 property and event, conversions in the last 90 days, and whether ads-pilot can change it |
| `set_conversion_action_status` | Enable or remove actions (level `full`) |
| `set_conversion_action_settings` | Primary or secondary, default value, always use default value, counting, click-through and view-through window, minimum call length (level `full`) |

## Rules

- **Removing is final.** Google cannot enable a removed action again, so `revert_change` cannot undo it. Removing is only allowed when the action had no conversions in the last `conversion_remove_lookback_days` days (profile, default 90).
- **Hiding is not possible through the API.** Google rejects it (`BLOCKED_VALUE`). Hide actions in Google Ads, or make them secondary (`primary_for_goal=false`) so they no longer steer bidding.
- **Goal protection.** If a change leaves a conversion goal (category plus origin, e.g. "Submit lead form, website") without any enabled primary action, the dry run names every campaign that bids on that goal. If one of them uses smart bidding, the change is blocked unless `allow_goal_loss=true`.
- **Secondary to primary:** the dry run warns that smart bidding campaigns on that goal start learning again.
- **Value and counting changes** apply from now on. Past conversions are not recalculated.
- **Not part of ads-pilot:** creating conversion actions (tag, code snippet), account settings for conversions (enhanced conversions, consent, customer match), offline conversion uploads, changing campaign goals.

## What the API allows per type

Tested with validate-only dry runs against a real account (Google Ads API v26, November 2026), unless marked "docs".

| Type | Enable | Remove | Settings |
|---|---|---|---|
| `WEBPAGE` (Google tag) | yes | yes | all |
| `GOOGLE_ANALYTICS_4_CUSTOM`, `_PURCHASE` | yes | yes | primary, value, view-through window. **Not** counting type and click-through window (they come from GA4) |
| `UNIVERSAL_ANALYTICS_GOAL`, `_TRANSACTION` | no | no | no. The API refuses every change (`MUTATE_NOT_ALLOWED`); remove them in Google Ads under Goals > Conversions |
| `AD_CALL`, `WEBSITE_CALL` (docs) | yes | yes | all, incl. minimum call length (0 to 10000 seconds) |
| `CLICK_TO_CALL`, `UPLOAD_CLICKS`, `WEBPAGE_CODELESS` (docs) | yes | yes | all except call length |
| `GOOGLE_HOSTED` and other Google-owned actions (local actions, store visits, app installs) | no | no | no (refused by ads-pilot) |
| Any type: status `HIDDEN` | | | cannot be set through the API |

The same table is in the code (`conversions.TYPE_SUPPORT`). Where the docs and a real dry run disagree, the dry run wins: ads-pilot always lets Google validate before anything changes.
