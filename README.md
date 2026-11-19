# ads-pilot-mcp

**Steer Google Ads through Claude, with guardrails built for agencies.**

ads-pilot-mcp lets Claude work in Google Ads accounts without touching the web interface: report, pause or enable keywords and ads, add keywords, adjust bids by device and time of day, refine ad copy.

Built for anyone who manages several client accounts. Every change starts as a dry run showing the old and the new value. It only runs after confirmation, and only in accounts the profile allows. Everything goes into a change log. Budgets change only within the limits you set per profile; deleting and billing are deliberately out of reach.

Small and local: the server runs on your own machine with your own Google Cloud project, and the code is quick to read. For reporting only, Google's own [Google Ads MCP server](https://github.com/googleads/google-ads-mcp) is a good choice too.

> **Deutsch:** ads-pilot-mcp lässt Claude in Google-Ads-Konten arbeiten, ohne die Weboberfläche zu bedienen: auswerten, Keywords und Anzeigen pausieren oder aktivieren, Keywords ergänzen, Gebote nach Gerät und Uhrzeit anpassen, Anzeigentexte überarbeiten. Jede Änderung startet als Probelauf mit altem und neuem Wert, wird erst nach Bestätigung ausgeführt, nur in freigegebenen Konten, und landet im Änderungsprotokoll. Mehr dazu: [docs/positioning.de.md](docs/positioning.de.md).

## What Claude can do

**Read and report** (Google Ads API v26)

| Tool | What it does |
|---|---|
| `show_profile` | Active profile, default account, accounts it may change, all limits |
| `list_accounts` | All Google Ads accounts the login can see, incl. clients under a manager account |
| `run_gaql` | Any read-only GAQL query; money also as normal amounts plus currency; suggests valid fields on errors |
| `describe_resource` | Fields, segments, metrics and enum values of a resource, to write GAQL that works |
| `campaign_overview` | Campaigns with type, status, budget, bid strategy, cost, clicks, conversions, cost per conversion |
| `search_terms_report` | Search terms of Search and Performance Max, e.g. "cost over X without conversion" |
| `pmax_channel_report` | Performance Max by channel (Search, YouTube, Discover, Maps, ...) with conversions per action |
| `targeting_overview` | Locations, languages, networks, location assets per campaign; warns about campaigns running worldwide |
| `recent_changes` | Latest executed changes from the change log, with their `change_id` |

**Change** (every one: dry run first, then confirm)

| Tool | What it does |
|---|---|
| `set_status` | Pause or enable keywords, ads, ad groups, campaigns and Performance Max asset groups |
| `add_keywords` | Add keywords (exact, phrase, broad) to an ad group |
| `add_negative_keywords` | Negative keywords on a campaign (incl. Performance Max), ad group, negative keyword list or the account-level list |
| `remove_negative_keywords` | Remove negative keywords again, to undo mistakes |
| `manage_negative_keyword_list` | Create a negative keyword list, attach it to a campaign or detach it |
| `set_campaign_budget` | Daily budget of a campaign, within the profile's limits |
| `set_bidding_target` | Target CPA or target ROAS of the current strategy, within the profile's limits; never switches strategies |
| `set_keyword_bid` | Max CPC of keywords or ad groups with manual CPC bidding |
| `set_device_bid_adjustment` | Bid adjustment per device, -100% to +900% |
| `set_ad_schedule_bid_adjustment` | Bid adjustment per time window (ad schedule) |
| `set_location_targeting` | Add, exclude or remove locations (by ID or name), presence or interest; warns before a campaign would run worldwide |
| `set_language_targeting` | Add or remove target languages (by ID, code or name) |
| `set_network_settings` | Search partners and Display network on or off (Search campaigns only) |
| `update_responsive_search_ad` | Final URL, headlines, descriptions of a responsive search ad |
| `update_asset_group_text_assets` | Headlines, long headlines, descriptions, business name of a Performance Max asset group |
| `set_search_themes` | Add or remove search themes of a Performance Max asset group |
| `set_brand_exclusions` | Attach or detach brand exclusion lists of a Performance Max campaign |
| `revert_change` | Put the old value of one logged change back, if nobody changed it in the meantime |

## Guardrails

1. **Dry run by default.** A write tool first only validates with Google and returns every change with old and new value plus a `confirm_token`. It executes only when called again with `confirm=true` and that token. The token is valid for 15 minutes and no longer fits if the arguments or the account changed in between.
2. **Allowlist per profile.** Writes go only to `allowed_customer_ids`. Claude cannot change that list.
3. **One profile per login.** Chosen at startup. Every response names profile and customer ID.
4. **No deleting.** Only pause and enable. Exceptions that undo mistakes or change targeting: removing negative keywords, locations, languages and search themes, detaching lists, unlinking replaced text assets (the assets stay). No billing, users or account settings.
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
allowed_customer_ids:                # the ONLY accounts Claude may change
  - "123-456-7890"
```

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
| `not in allowed_customer_ids` | Intended. Add the account to the profile if it should be changeable. |
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
