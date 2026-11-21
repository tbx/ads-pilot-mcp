# ads-pilot-mcp

**Steer Google Ads through Claude, with guardrails built for agencies.**

ads-pilot-mcp lets Claude work in Google Ads accounts without touching the web interface: report, pause or enable keywords and ads, add keywords, adjust bids and bid strategies, refine ad copy, tidy up conversion actions.

Built for anyone who manages several client accounts. Every change starts as a dry run showing the old and the new value. It only runs after confirmation, and only within the access level the profile sets per account (read only, standard or full). Everything goes into a change log. Budgets change only within the limits you set per profile; deleting campaigns, ads or keywords, billing and user management are deliberately out of reach.

Small and local: the server runs on your own machine with your own Google Cloud project, and the code is quick to read. For reporting only, Google's own [Google Ads MCP server](https://github.com/googleads/google-ads-mcp) is a good choice too.

> **Deutsch:** ads-pilot-mcp lässt Claude in Google-Ads-Konten arbeiten, ohne die Weboberfläche zu bedienen: auswerten, Keywords und Anzeigen pausieren oder aktivieren, Keywords ergänzen, Gebote und Gebotsstrategien anpassen, Anzeigentexte überarbeiten, Conversion-Aktionen aufräumen. Jede Änderung startet als Probelauf mit altem und neuem Wert, wird erst nach Bestätigung ausgeführt, nur im Rahmen der Zugriffsstufe pro Konto (nur lesen, Standard oder voll), und landet im Änderungsprotokoll. Mehr dazu: [docs/positioning.de.md](docs/positioning.de.md).

## What Claude can do

**Read and report** (Google Ads API v26)

| Tool | What it does |
|---|---|
| `show_profile` | Active profile, default account, access level per account, all limits |
| `list_accounts` | All Google Ads accounts the login can see, incl. clients under a manager account |
| `run_gaql` | Any read-only GAQL query; money also as normal amounts plus currency; suggests valid fields on errors |
| `describe_resource` | Fields, segments, metrics and enum values of a resource, to write GAQL that works |
| `campaign_overview` | Campaigns with type, status, budget, bid strategy, cost, clicks, conversions, cost per conversion |
| `search_terms_report` | Search terms of Search and Performance Max, e.g. "cost over X without conversion" |
| `pmax_channel_report` | Performance Max by channel (Search, YouTube, Discover, Maps, ...) with conversions per action |
| `targeting_overview` | Locations, languages, networks, location assets per campaign; warns about campaigns running worldwide |
| `list_conversion_actions` | Conversion actions with type, status, primary or secondary, counting, value, windows, conversions, and whether ads-pilot can change them |
| `recent_changes` | Latest executed changes from the change log, with their `change_id` |

**Change** (every one: dry run first, then confirm; level = access level the account needs)

| Tool | Level | What it does |
|---|---|---|
| `set_status` | standard | Pause or enable keywords, ads, ad groups, campaigns and Performance Max asset groups |
| `add_keywords` | standard | Add keywords (exact, phrase, broad) to an ad group |
| `add_negative_keywords` | standard | Negative keywords on a campaign (incl. Performance Max), ad group, negative keyword list or the account-level list |
| `remove_negative_keywords` | standard | Remove negative keywords again, to undo mistakes |
| `manage_negative_keyword_list` | standard | Create a negative keyword list, attach it to a campaign or detach it |
| `set_campaign_budget` | standard | Daily budget of a campaign, within the profile's limits |
| `set_bidding_target` | standard | Target CPA or target ROAS of the current strategy, within the profile's limits; never switches strategies |
| `set_keyword_bid` | standard | Max CPC of keywords or ad groups with manual CPC bidding |
| `set_bidding_strategy` | full | Switch the bid strategy: Maximize conversions (opt. target CPA), Maximize conversion value (opt. target ROAS), Maximize clicks (opt. max CPC), Manual CPC, Target impression share. Performance Max: the two Maximize conversion strategies |
| `set_conversion_action_status` | full | Enable or remove conversion actions; removing only without conversions in the last 90 days, blocked when a smart bidding campaign would lose its last primary action, unless explicitly allowed |
| `set_conversion_action_settings` | full | Primary or secondary, value, counting, conversion windows, minimum call length |
| `set_device_bid_adjustment` | standard | Bid adjustment per device, -100% to +900% |
| `set_ad_schedule_bid_adjustment` | standard | Bid adjustment per time window (ad schedule) |
| `set_location_targeting` | standard | Add, exclude or remove locations (by ID or name), presence or interest; warns before a campaign would run worldwide |
| `set_language_targeting` | standard | Add or remove target languages (by ID, code or name) |
| `set_network_settings` | standard | Search partners and Display network on or off (Search campaigns only) |
| `update_responsive_search_ad` | standard | Final URL, headlines, descriptions of a responsive search ad |
| `update_asset_group_text_assets` | standard | Headlines, long headlines, descriptions, business name of a Performance Max asset group |
| `set_search_themes` | standard | Add or remove search themes of a Performance Max asset group |
| `set_brand_exclusions` | standard | Attach or detach brand exclusion lists of a Performance Max campaign |
| `revert_change` | as original | Put the old value of one logged change back, if nobody changed it in the meantime |

Details on conversion actions, including what the API allows per type: [docs/conversions.md](docs/conversions.md).

## Guardrails

1. **Dry run by default.** A write tool first only validates with Google and returns every change with old and new value plus a `confirm_token`. It executes only when called again with `confirm=true` and that token. The token is valid for 15 minutes and no longer fits if the arguments or the account changed in between.
2. **Access level per account,** modelled on Google Ads' own: `read_only` (reading and reports), `standard` (the daily work), `full` (also bid strategies and conversion actions). Accounts not listed are read only. Claude cannot change the levels; only the profile file can.
3. **One profile per login.** Chosen at startup. Every response names profile and customer ID.
4. **No deleting of campaigns, ad groups, ads or keywords,** only pause and enable. Removing works for things you can add back (negative keywords, locations, languages, search themes, list links, replaced text assets). The one final removal: conversion actions without data, at level `full`. No billing, users or account settings.
5. **Budget limits.** Campaign budgets only up to `max_daily_budget` and at most `max_budget_change_percent` per step (default 50%), set in the profile. No shared budgets.
6. **Change log.** Every executed change goes to `logs/changes.jsonl` with a unique `change_id`, time, profile, account, tool, object type and ID, name, old and new value.
7. **Partial failures are reported.** Where several items go in one call (e.g. negative keywords), good ones go through and each failing one is named with Google's error code and a short explanation.
8. **Undo.** `revert_change` puts the old value back after a dry run, and refuses if someone changed the value since.

## Installation

Requirements: macOS or Linux, [uv](https://docs.astral.sh/uv/) (on a Mac: `brew install uv`). uv brings the right Python version along.

```bash
git clone https://github.com/tbx/ads-pilot-mcp.git
cd ads-pilot-mcp
uv sync
```

No developer token is needed. Google retired it in September 2026; API access now belongs to your Google Cloud project.

## Set up

**[docs/setup.md](docs/setup.md)** walks you through everything, click by click (about 15 minutes the first time): Google Cloud project, sign-in, profile, test, Claude Desktop.

A profile is one Google login plus the accounts it works with, usually one per client. It is a short file:

```yaml
auth:                                # filled in by: uv run ads-pilot-login <name>
  client_id: ""
  client_secret: ""
  refresh_token: ""
login_customer_id: ""                # manager account (MCC) ID, or "" if none
default_customer_id: "123-456-7890"  # account used when none is named
accounts:                            # access level per account; all others are read only
  "123-456-7890": standard           # read_only, standard or full
```

Older profiles with `allowed_customer_ids` keep working: those accounts count as `full`.

Profile files stay on your machine. `profiles/*` is ignored by git, except the example.

## Test without changing anything

```bash
ADS_PILOT_PROFILE=acme uv run ads-pilot-check
```

It signs in, reads the default account and lets Google validate a write as a dry run. Expected last line: `All good. Nothing was changed.`

Offline tests (no Google account needed):

```bash
uv run pytest
```

## Connect to Claude Desktop

Details in [docs/setup.md](docs/setup.md#step-6-connect-to-claude-desktop). In short: Claude Desktop > Settings > Developer > Edit Config, add one entry per profile (full paths; find uv's path with `which uv`):

```json
{
  "mcpServers": {
    "ads-pilot-acme": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["--directory", "/full/path/to/ads-pilot-mcp", "run", "ads-pilot-mcp"],
      "env": { "ADS_PILOT_PROFILE": "acme" }
    }
  }
}
```

Restart Claude Desktop. Then ask for example: *"Show my ads-pilot profile"* or *"Which keywords in campaign X had no clicks last month?"*

Claude Code: `claude mcp add ads-pilot-acme -e ADS_PILOT_PROFILE=acme -- uv --directory /full/path/to/ads-pilot-mcp run ads-pilot-mcp`

## Landing page

`site/` is the page at https://tbx.github.io/ads-pilot-mcp/, published by `.github/workflows/pages.yml` on every push that touches `site/`. After changing the hero headline, render the share image again:

```bash
uv run python scripts/og/render.py
```

## Logs

| File | Content |
|---|---|
| `logs/changes.jsonl` | Every executed change (one JSON line each) |
| `logs/ads-pilot.log` | Every tool call with profile and customer ID, errors |

## Troubleshooting

| Message | Fix |
|---|---|
| `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` | Your Cloud project only has Test access. Profile step 1e: apply for Explorer. |
| `The Google sign-in has expired or was revoked` | Run `uv run ads-pilot-login <profile>` again. |
| `is read_only in profile` / `needs full` | Intended. Raise the account's level under `accounts` in the profile, then restart Claude Desktop. |
| `USER_PERMISSION_DENIED` | The Google login has no (or read-only) access to that account, or a manager account is needed: set `login_customer_id`. |

## Roadmap

1. **Optimize** (this version)
2. **Build:** create complete Search campaigns, started paused, budget capped per profile
3. **Extend:** create Performance Max and other campaign types, image and video assets, keyword research
4. **Accompany:** recurring checks and reports; Claude proposes, a human approves

**Option at any stage: run it on your own server.** Today Claude Desktop starts ads-pilot on your Mac. Run on your own server over HTTPS with a sign-in, it can be added to Claude as a custom connector and used from claude.ai, the mobile app and by colleagues. Still self-hosted, still your own Google Cloud project, same guardrails.

## Status

Early version. Tested against Google Ads API v26 (google-ads 35.1.0) and FastMCP 4.2.

## License

ads-pilot-mcp is free software under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only).

- **Using it**, also inside your agency and for your clients: free, no strings attached.
- **Changing it** for your own use: free. If you offer a modified version to others as a network service, the AGPL requires you to publish your changes under the same license.
- **Commercial license:** want to build ads-pilot into a closed product or a hosted service without the AGPL obligations? A commercial license is available from the author, Tobias Leingruber ([leingruberconsulting.com](https://leingruberconsulting.com)).

The name "ads-pilot" is not covered by the license. Forks and derived services need a different name.

Contributions are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).
