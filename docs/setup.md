# Setup guide

From zero to "Claude works in my Google Ads account". About 15 minutes the first time.

You need: a Mac, the Google account that has access to your Google Ads accounts, and ads-pilot-mcp installed (see [README](../README.md#installation)).

| Step | What | How often |
|---|---|---|
| [1](#step-1-create-a-google-cloud-project) | Create a Google Cloud project | once |
| [2](#step-2-create-a-login-for-ads-pilot) | Create a login for ads-pilot | once |
| [3](#step-3-create-a-profile-and-sign-in) | Create a profile and sign in | per profile |
| [4](#step-4-fill-in-the-accounts) | Fill in the accounts | per profile |
| [5](#step-5-test) | Test | per profile |
| [6](#step-6-connect-to-claude-desktop) | Connect to Claude Desktop | per profile |

A profile is one Google login plus the Google Ads accounts it works with. Usually you create one per client.

---

## Step 1: Create a Google Cloud project

We recommend a new project just for ads-pilot. It keeps your Google Ads access separate from everything else.

> **Exception:** your company already uses a Google Cloud project with the Google Ads API at access level "Basic" or "Standard"? Use that one instead (a new project starts lower) and continue at d).

**a)** Open <https://console.cloud.google.com> and sign in with the Google account that has access to your Google Ads accounts.

**b)** Click the project name at the top left > **New project**.
- Project name: `ads-pilot`
- Organization / Location: leave as suggested (often "No organization")
- Click **Create**.

**c)** Wait a few seconds, click the project name at the top left again and select **ads-pilot**.

**d)** Type "Google Ads API" in the search bar at the top, open it and click **Enable**. When it worked, the page shows "Status: Enabled".
- Do not click "Disable API" at the top.
- Ignore the "Create credentials" button. Step 2 creates the right kind of login in the right order.

**e)** On the same page, find the box **Access levels** and click **Manage**. Look under "Current access level":
- **Explorer**, **Basic** or **Standard**: all good, go to step 2.
- **Test**: in the box "Upgrade access level" (next level: Explorer) click **Apply for access** and answer the questions. Google shows "currently under review" for a few minutes. You do not have to wait: continue with step 2 and check back later. The test in step 5 only works once the page shows "Explorer".

Lost the page? Menu **APIs & Services** > **Enabled APIs & services** > scroll down past the charts to the table > **Google Ads API**.

No developer token is needed. Google retired it in September 2026.

---

## Step 2: Create a login for ads-pilot

**a) Consent screen.** Menu **APIs & Services** > **OAuth consent screen**. This opens "Google Auth Platform". Click **Get started**:
- App Information: App name `ads-pilot`, support email = your email
- Audience: **External** ("Internal" only exists with Google Workspace)
- Contact Information: your email
- Finish: tick the "Google API Services: User Data Policy" box > **Continue** > **Create**

You land on "OAuth Overview" with a menu on the left (Branding, Audience, Clients, Data Access). Ignore "Create OAuth client" for now.

**b) Google Ads access.** Left menu **Data Access** > **Add or remove scopes**. Type `adwords` in the filter and tick **Google Ads API** (`.../auth/adwords`). Not listed? Scroll down to "Manually add scopes", paste

```
https://www.googleapis.com/auth/adwords
```

and click **Add to table**. Then **Update**, scroll down and click **Save**.

Google describes this scope as "See, edit, create, and delete your Google Ads accounts". That is Google's standard text and there is no narrower one. ads-pilot itself never deletes and only changes the accounts you allow in step 4.

**c) Test user and publishing.** Left menu **Audience**:
1. Under "Test users" click **+ Add users**, enter the email of your Google account, click **Save**. Required while the app is in "Testing".
2. Recommended: publish the app, otherwise the sign-in expires after about 7 days (ads-pilot then tells you so and you repeat step 3).
   - Left menu **Branding**: fill in what is missing, typically app home page and privacy policy link (pages on your own website), authorized domain (that website's domain without `https://`) and developer contact email. **Leave "App logo" empty:** a logo makes Google require a review, which can take days. Click **Save**.
   - Left menu **Audience** > **Publish app** > confirm. The status now reads "In production".
   - A yellow banner "Your app requires verification" appears. Ignore it. A Google review is only needed for apps offered to the public; your own use works without it (up to 100 users).

**d) Client.** Left menu **Clients** > **+ Create client**:
- Application type: **Desktop app**
- Name: `ads-pilot`
- Leave **"Use this client for an AI-powered agent" unticked.** It is meant for Google's own hosted MCP tools; ads-pilot uses the regular Google Ads API.
- Click **Create**.

**e) Download.** A window "OAuth client created" opens. Click **Download JSON** now: once you close the window, Google never shows the secret again. The file lands in Downloads and starts with `client_secret_`. Then click **OK**.

Treat this file like a password: do not share it, do not take screenshots of that window, never upload it anywhere.

---

## Step 3: Create a profile and sign in

Open the Terminal app. Type `cd ` (with a space), drag the ads-pilot-mcp folder from Finder into the Terminal window, press Enter.

Create the profile (here called `acme`, use any short name):

```bash
cp profiles/example.yaml profiles/acme.yaml
```

Sign in:

```bash
uv run ads-pilot-login acme
```

It uses the newest `client_secret_*.json` from your Downloads folder. (Stored it elsewhere? Type the command, a space, and drag the file into the Terminal window.)

A browser window opens:
1. Choose the Google account that has access to your Google Ads accounts.
2. "Google hasn't verified this app" names you as the developer. That is normal for your own app: click **Go to ads-pilot (unsafe)** in the red box (on some screens first **Advanced**).
3. Google lists broad rights (budgets, billing, deleting). That is the only Google Ads permission Google offers. ads-pilot itself never deletes, never touches billing, changes budgets only within the limits in your profile, and it can never do more than your Google account. Click **Continue**.

The Terminal shows "Signed in". The program has written `client_id`, `client_secret` and `refresh_token` into `profiles/acme.yaml`. You do not need to copy anything.

- **Never show or send the `refresh_token`.** It is the key to your Google Ads accounts.
- Keep the JSON file, but move it out of Downloads to a safe place (password manager or e.g. `~/Documents/ads-pilot/`). **Not** into the ads-pilot-mcp folder. You need it again only to sign in anew or for another profile.
- Extra safety: sign in with a Google account that has "Standard" instead of "Admin" access in Google Ads.

---

## Step 4: Fill in the accounts

Open `profiles/acme.yaml` in a text editor (e.g. TextEdit). Account IDs are shown at the top of the Google Ads screen, next to the account name (format `123-456-7890`).

| Line | What to enter |
|---|---|
| `default_customer_id` | The account Claude works in when you do not name another one |
| `accounts` | One line per account Claude may change, with its access level (see below). Claude reads every account your login sees, but changes only these. |
| `login_customer_id` | Only if you reach the account through a manager account (MCC): the manager account's ID. Otherwise leave `""`. |
| `max_daily_budget` | The highest daily budget Claude may set for a campaign, in the account currency. Remove the line to switch budget changes off. |
| `max_budget_change_percent` | The largest budget change per step, in percent (default 50). |
| `max_negative_keywords_per_call` | The most negative keywords Claude may add or remove in one call (default 50). |
| `allow_campaign_status_change` | `false` stops Claude from pausing or enabling whole campaigns (default `true`). |
| `allow_location_changes` | `false` stops Claude from changing location targeting (default `true`). |
| `max_target_change_percent` | The largest change of a target CPA or ROAS per step, in percent (default 30). |
| `max_target_cpa` | The highest target CPA Claude may set. Empty = only the percent limit. |
| `max_cpc` | The highest max CPC for manual bids, and the required cap for Maximize clicks. Empty = no cap (Claude then warns above double the current bid). |
| `allowed_bidding_strategies` | Optional list, e.g. `[MAXIMIZE_CONVERSIONS, TARGET_SPEND]`: the only strategies Claude may switch to. Empty = all supported. |
| `conversion_remove_lookback_days` | Conversion actions are removed only if they had no conversions in this many days (default 90). |
| `max_conversion_actions_per_call` | The most conversion actions per call (default 50). |

**Access levels.** Modelled on the levels in Google Ads (Admin, Standard, Read only):

| Level | What Claude may do |
|---|---|
| `read_only` | Read and report only. Every account that is not listed has this level. |
| `standard` | The daily work: pause and enable, keywords, negative keywords, ad and asset texts, bids and targets, budgets, locations and languages. |
| `full` | Everything in `standard`, plus switching bid strategies and changing or removing conversion actions. |

```yaml
accounts:
  "123-456-7890": full        # my own account
  "234-567-8901": standard    # client A, daily care
  "345-678-9012": read_only   # client B, reports only
```

Claude cannot change these levels. To change one, edit the file and restart Claude Desktop (Cmd+Q). The limits below apply at every level. Older profiles with `allowed_customer_ids` keep working: those accounts count as `full`.

**Manager account or not?** In Google Ads, click the account name at the top right. If you first see a manager account with the client accounts below it, you use an MCC. Not sure? Leave it empty; the test in step 5 lists every account your login can see.

---

## Step 5: Test

Changes nothing in your accounts:

```bash
ADS_PILOT_PROFILE=acme uv run ads-pilot-check
```

The last line should read `All good. Nothing was changed.` If not, see [Troubleshooting](../README.md#troubleshooting).

---

## Step 6: Connect to Claude Desktop

**a)** Claude Desktop > **Settings** > **Developer** > **Edit Config**. Finder shows `claude_desktop_config.json`. Open it with TextEdit.

**b)** Add this block inside the outer `{ }`, next to what is already there, with a comma between blocks. Replace the folder path with yours; find uv's path with `which uv`.

```json
"mcpServers": {
  "ads-pilot-acme": {
    "command": "/opt/homebrew/bin/uv",
    "args": ["--directory", "/full/path/to/ads-pilot-mcp", "run", "ads-pilot-mcp"],
    "env": { "ADS_PILOT_PROFILE": "acme" }
  }
}
```

Already have `"mcpServers"`? Then only add the `"ads-pilot-acme": { ... }` part inside it.

**c)** Save, quit Claude Desktop completely (**Cmd+Q**) and open it again.

**d)** In a new chat ask: *"Show my ads-pilot profile"*. Claude answers with the profile name and your default account. Done.

---

## Advanced: service account instead of your own login

Only for IT setups. In the profile, replace the three lines under `auth:` with

```yaml
  json_key_file_path: "/full/path/to/service-account-key.json"
```

and add the service account's email address as a user in Google Ads (Admin > Access and security), access level "Standard".
