# ads-pilot-mcp: Positioning

As of 18 Nov 2026 · Deutsche Fassung: [positioning.de.md](positioning.de.md)

## In one sentence

**Steer Google Ads through Claude, with guardrails built for agencies.**

## Who it is for

Agencies, consultants and in-house teams who manage several Google Ads accounts, often through a manager account (MCC), and who want Claude to do real work in those accounts without giving up control.

## The problem

Anyone working in Google Ads with Claude today chooses between:

- **Hosted services.** They do a lot, but credentials and account data pass through someone else's server.
- **Large open-source servers.** They cover many platforms with a great many tools. Powerful, but hard to oversee and hard to review.
- **Reporting only.** Safe, but every change is still manual work in the web interface.

Agencies face one more risk that hardly any tool guards against on purpose: **a change in the wrong client account.** Through an MCC, one login reaches every client, and the Google Ads API will not stop a change in the wrong account.

## The answer

| Principle | What it means |
|---|---|
| **Small** | A few tools for day-to-day account management. Claude reliably picks the right one, and the context stays free for the actual work. |
| **Local** | Runs on your own machine with your own Google Cloud project. No service in between, no data with third parties. |
| **Auditable** | The whole codebase can be read in an hour. Whoever runs it knows what it does. |
| **Guardrails** | A dry run before every change, an allowlist per profile, no deleting, a log with old and new values. |

## The guardrails in detail

1. **Dry run by default.** Every change is validated first and shows the old and the new value. It only runs after explicit confirmation, within 15 minutes of the dry run.
2. **Allowlist per profile.** Write access only to accounts listed in the profile. Claude cannot change that list.
3. **One profile per client.** The profile is chosen at startup. Every response names the profile and the customer ID.
4. **No deleting.** Campaigns, ad groups, ads and keywords are only paused. Removing works only for things you can add back, such as negative keywords or locations. Every logged change can be undone with one call.
5. **No sensitive areas.** No billing, users or account settings. Campaign budgets only up to a cap per profile and in limited steps.
6. **A complete change log.** Every executed change with time, profile, account, object, old and new value. Easy to show to the client and to reverse by hand if needed.

## What it does

**Version 1: Optimize** (available)

- List accounts, run free-form queries (GAQL)
- Pause or enable keywords, ads, ad groups, campaigns and Performance Max asset groups
- Add keywords to ad groups
- Bid adjustments for devices and times of day (ad schedule)
- Change final URL, headlines and descriptions of Responsive Search Ads
- Daily campaign budgets, within the profile's limits
- Negative keywords on campaign, ad group, list and account level, also for Performance Max
- Location and language targeting, Search partners and Display network
- Target CPA and target ROAS, max CPC for manual bids
- Performance Max: text assets, search themes, brand exclusions
- Undo any logged change with one call
- Ready-made reports: campaigns, search terms (incl. Performance Max), Performance Max channels, targeting

**Roadmap**

| Version | Claude's role | Scope |
|---|---|---|
| 2: Build | Campaign manager | Create complete Search campaigns. New campaigns start paused; budgets only at creation and only up to a cap per profile. |
| 3: Extend | Specialist | Create Performance Max and other campaign types, image and video assets, keyword research |
| 4: Accompany | Account manager | Recurring checks and reports. Claude proposes, a human approves. |

**Option at any stage: your own server.** Today Claude Desktop starts ads-pilot locally on your Mac. Run on your own server (HTTPS, with a sign-in), it becomes a custom connector in Claude: usable from claude.ai in the browser, the mobile app and by colleagues, and the basis for scheduled checks in version 4. It stays self-hosted, with your own Google Cloud project.

The guardrails apply in every version and on every setup.

## What it deliberately is not

- **Not a hosted service.** Every user brings their own Google Cloud project and runs ads-pilot on their own Mac or server.
- **Not an all-in-one tool for every ad platform.** Google Ads only, done thoroughly.
- **Not an autopilot.** Claude proposes and executes, a human approves.

## Where it fits

For orientation, not judgement:

- **Google's own Google Ads MCP server** ([googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp)) is excellent for reporting. So far it only writes assets and labels.
- **Hosted services** in the Claude directory, such as Adspirer, AdCopilot or Windsor.ai, offer many platforms and many features. Accounts are connected through the respective provider.
- **Extensive open-source servers** such as [AdLoop](https://github.com/kLOsk/adloop) run locally, are versatile, cover several platforms and evolve quickly.

ads-pilot-mcp deliberately chooses less scope, calm over speed, and protection against changes in the wrong account.

## Copy to use

**GitHub description (one line)**

> Local MCP server for Google Ads with dry runs, an account allowlist and a change log.

**Short intro for the README (English)**

> **ads-pilot-mcp** lets Claude work in Google Ads accounts without touching the web interface: report, pause or enable keywords and ads, add keywords, adjust bids by device and time of day, refine ad copy.
>
> Built for anyone who manages several client accounts. Every change starts as a dry run showing the old and the new value. It only runs after confirmation, and only in accounts the profile allows. Everything goes into a change log. Budgets change only within the limits you set per profile; deleting and billing are deliberately out of reach.
>
> Small and local: the server runs on your own machine with your own Google Cloud project, and the code is quick to read.
>
> For reporting only, Google's own [Google Ads MCP server](https://github.com/googleads/google-ads-mcp) is a good choice too.

**Kurzes Intro (Deutsch)**

> **ads-pilot-mcp** lässt Claude in Google-Ads-Konten arbeiten, ohne die Weboberfläche zu bedienen: auswerten, Keywords und Anzeigen pausieren oder aktivieren, Keywords ergänzen, Gebote nach Gerät und Uhrzeit anpassen, Anzeigentexte überarbeiten.
>
> Gebaut für alle, die mehrere Kundenkonten betreuen. Jede Änderung startet als Probelauf mit altem und neuem Wert. Ausgeführt wird erst nach Bestätigung und nur in Konten, die im Profil freigegeben sind. Alles landet in einem Änderungsprotokoll. Budgets ändern sich nur innerhalb der Grenzen, die Sie pro Profil festlegen; Löschen und Abrechnung sind bewusst nicht möglich.
>
> Klein und lokal: Der Server läuft auf dem eigenen Rechner mit dem eigenen Google-Cloud-Projekt, und der Code ist schnell gelesen.
>
> Für reine Auswertungen empfiehlt sich auch Googles eigener [Google Ads MCP Server](https://github.com/googleads/google-ads-mcp).

## License and business model

**Open source under AGPL-3.0, commercial license on request.**

- Agencies, consultants and in-house teams use ads-pilot for free, for themselves and their clients.
- Whoever turns a modified ads-pilot into a network service must publish their changes. Nobody can quietly build a closed paid service on top of it.
- For closed products or hosted services there is a commercial license from the author (dual licensing).
- The name "ads-pilot" stays with the author. Forks need a different name.

Comparable projects almost all use MIT (Google's own server: Apache 2.0). AdLoop, for example, is MIT and earns money with a hosted cloud version. ads-pilot starts with the stronger protection and can still loosen it later; the reverse is not possible.

## Open points

- README in English, with a German version of the marketing copy.
- Re-check the market overview before publishing, since these projects move fast.
- Set up the CLA text before accepting the first outside contribution.
- Decide the contact for commercial license requests (website or email).
