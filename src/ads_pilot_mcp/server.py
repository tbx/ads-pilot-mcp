# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""MCP server: tools Claude can call. Every answer names profile and customer ID."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from . import bidding, negatives, pmax, reports, revert, targeting, writes
from .ads import API_VERSION
from .ads import Ads, AdsError
from .config import ConfigError, Profile, format_customer_id, load_profile, log_dir
from .writes import TOKEN_TTL_SECONDS, WriteRefused, Writer

log = logging.getLogger("ads_pilot")

MAX_ACCOUNTS = 50
MAX_ROWS_LIMIT = 1000

INSTRUCTIONS = """\
Google Ads tools. Every response contains 'profile' and 'customer_id': always tell the
user which profile and which account you worked in. customer_id is optional and falls
back to the profile's default account. login_customer_id is only needed when the account
is reached through a manager account (MCC) and the profile does not set it already.

Changing things always takes two calls:
1. Call the write tool without confirm. This is a dry run: nothing changes, Google
   validates the change, and you get every change with old and new value plus a
   confirm_token. Show the changes to the user in a clear list.
2. Only after the user explicitly says yes to exactly these changes, call the same tool
   again with the same arguments, confirm=true and the confirm_token.
Never set confirm=true on your own initiative. Find IDs with run_gaql first.
Deleting, billing, users and account settings are not available on purpose; the only removals are
negative keywords and negative keyword list links (to undo mistakes). Campaign budgets
only within the profile's max_daily_budget and max_budget_change_percent.
"""

Status = Literal["PAUSED", "ENABLED"]
Day = Literal["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
NegLevel = Literal["campaign", "ad_group", "shared_list", "account"]
DateRange = Literal["TODAY", "YESTERDAY", "LAST_7_DAYS", "LAST_14_DAYS", "LAST_30_DAYS", "THIS_MONTH", "LAST_MONTH"]


class NegativeKeywordIn(BaseModel):
    text: str
    match_type: Literal["EXACT", "PHRASE", "BROAD"]


class KeywordIn(BaseModel):
    text: str
    match_type: Literal["EXACT", "PHRASE", "BROAD"] = "PHRASE"



def setup_logging() -> None:
    """Log to stderr (stdout belongs to the MCP protocol) and to logs/ads-pilot.log."""
    if log.handlers:
        return
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(sys.stderr), logging.FileHandler(folder / "ads-pilot.log", encoding="utf-8")):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def create_server(profile: Profile, ads: Ads | None = None, log_folder: Path | None = None) -> FastMCP:
    ads = ads or Ads(profile)
    writer = Writer(profile, ads, log_folder or log_dir())
    mcp = FastMCP("ads-pilot", instructions=INSTRUCTIONS)

    def where(customer_id: str | None, login_customer_id: str | None = None) -> dict[str, Any]:
        info: dict[str, Any] = {"profile": profile.name, "customer_id": format_customer_id(customer_id) if customer_id else None}
        if login_customer_id:
            info["login_customer_id"] = format_customer_id(login_customer_id)
        return info

    def fail(tool: str, customer_id: str | None, error: Exception) -> ToolError:
        log.error("profile=%s customer=%s tool=%s ERROR %s", profile.name, customer_id or "-", tool, error)
        return ToolError(f"[profile {profile.name}, customer {format_customer_id(customer_id) if customer_id else '-'}] {error}")

    def resolve(customer_id: str | None, login_customer_id: str | None, tool: str) -> tuple[str, str | None]:
        try:
            return profile.resolve_customer_id(customer_id), profile.resolve_login_customer_id(login_customer_id)
        except ConfigError as e:
            raise fail(tool, None, e) from e

    @mcp.tool(annotations={"readOnlyHint": True})
    def show_profile() -> dict[str, Any]:
        """Show the active profile: default account, manager account and the accounts it may change."""
        log.info("profile=%s customer=%s tool=show_profile", profile.name, profile.default_customer_id or "-")
        return {
            "profile": profile.name,
            "customer_id": format_customer_id(profile.default_customer_id) if profile.default_customer_id else None,
            "login_customer_id": format_customer_id(profile.login_customer_id) if profile.login_customer_id else None,
            "writable_customer_ids": sorted(format_customer_id(c) for c in profile.allowed_customer_ids),
            "max_daily_budget": profile.max_daily_budget,
            "max_budget_change_percent": profile.max_budget_change_percent,
            "max_negative_keywords_per_call": profile.max_negative_keywords_per_call,
            "allow_campaign_status_change": profile.allow_campaign_status_change,
            "allow_location_changes": profile.allow_location_changes,
            "max_target_change_percent": profile.max_target_change_percent,
            "max_target_cpa": profile.max_target_cpa,
            "max_cpc": profile.max_cpc,
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_accounts() -> dict[str, Any]:
        """List the Google Ads accounts this profile can access, including client accounts
        directly below a manager account (MCC). 'writable' marks accounts this server may change."""
        log.info("profile=%s customer=- tool=list_accounts", profile.name)
        try:
            ids = ads.list_accessible_customer_ids()
        except AdsError as e:
            raise fail("list_accounts", None, e) from e

        accounts = []
        for cid in ids[:MAX_ACCOUNTS]:
            accounts.append(_describe_account(ads, profile, cid))
        return {
            "profile": profile.name,
            "customer_id": None,
            "accounts": accounts,
            "truncated": len(ids) > MAX_ACCOUNTS,
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    def run_gaql(
        query: str,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        max_rows: int = 200,
    ) -> dict[str, Any]:
        """Run a read-only Google Ads Query Language (GAQL) query against Google Ads API v26, e.g.
        "SELECT campaign.id, campaign.name, metrics.clicks FROM campaign WHERE segments.date DURING LAST_7_DAYS".
        customer_id defaults to the profile's account. max_rows is capped at 1000. Every *_micros value also comes
        as a normal amount (metrics.cost next to metrics.cost_micros), plus the account currency. On unknown fields
        the error names similar valid ones; describe_resource lists all fields, segments and metrics."""
        cid, login = resolve(customer_id, login_customer_id, "run_gaql")
        max_rows = max(1, min(int(max_rows), MAX_ROWS_LIMIT))
        log.info("profile=%s customer=%s tool=run_gaql query=%r", profile.name, cid, " ".join(query.split()))
        try:
            rows, truncated = ads.search(cid, query, login_customer_id=login, max_rows=max_rows)
        except AdsError as e:
            hints = reports.field_hints(ads, str(e))
            raise fail("run_gaql", cid, AdsError(" ".join([str(e), *hints]))) from e
        result = {**where(cid, login), "api_version": API_VERSION, "row_count": len(rows), "truncated": truncated,
                  "rows": reports.add_money(rows)}
        if any(k.endswith("_micros") for r in rows[:1] for k in r):
            try:
                result["currency"] = ads.currency(cid, login)
            except AdsError:
                pass
        return result

    def report(name, customer_id, login_customer_id, build) -> dict[str, Any]:
        cid, login = resolve(customer_id, login_customer_id, name)
        log.info("profile=%s customer=%s tool=%s", profile.name, cid, name)
        try:
            return {**where(cid, login), **build(cid, login)}
        except (AdsError, reports.ReportError) as e:
            raise fail(name, cid, e) from e

    @mcp.tool(annotations={"readOnlyHint": True})
    def describe_resource(resource: str, field: str | None = None) -> dict[str, Any]:
        """List the fields of a GAQL resource (e.g. 'campaign', 'search_term_view', 'asset_group') in Google Ads
        API v26, with the segments and metrics that can be selected with it and the valid values of enum fields.
        With field (e.g. 'segments.ad_network_type'), return the valid values of that one field. Use it before
        writing GAQL to avoid fields that do not exist (e.g. campaign.start_date) or invalid enum values."""
        log.info("profile=%s customer=- tool=describe_resource resource=%s", profile.name, resource)
        try:
            return {"profile": profile.name, "customer_id": None, "api_version": API_VERSION,
                    **reports.describe_resource(ads, resource, field)}
        except (AdsError, reports.ReportError) as e:
            raise fail("describe_resource", None, e) from e

    @mcp.tool(annotations={"readOnlyHint": True})
    def campaign_overview(
        date_range: DateRange = "LAST_30_DAYS",
        start_date: str | None = None,
        end_date: str | None = None,
        include_paused: bool = False,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
    ) -> dict[str, Any]:
        """Active campaigns (optionally also paused) with type, status, daily budget, bid strategy, cost, clicks,
        impressions, conversions, cost per conversion, conversion value and ROAS for a period, plus totals.
        Period: date_range, or start_date and end_date (YYYY-MM-DD)."""
        return report("campaign_overview", customer_id, login_customer_id,
                      lambda cid, login: reports.campaign_overview(ads, cid, login, date_range, start_date, end_date,
                                                                   include_paused))

    @mcp.tool(annotations={"readOnlyHint": True})
    def search_terms_report(
        campaign_id: str | None = None,
        date_range: DateRange = "LAST_30_DAYS",
        start_date: str | None = None,
        end_date: str | None = None,
        sort_by: Literal["cost", "conversions", "clicks"] = "cost",
        min_cost_without_conversion: float | None = None,
        limit: int = 100,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
    ) -> dict[str, Any]:
        """Search terms of Search campaigns (search_term_view) AND Performance Max (campaign_search_term_view)
        in one list, with cost, clicks, conversions and cost per conversion. Optionally one campaign. Filter
        min_cost_without_conversion: only terms that cost at least this much and brought no conversion
        (candidates for negative keywords). Sorted by cost, conversions or clicks. limit up to 1000."""
        return report("search_terms_report", customer_id, login_customer_id,
                      lambda cid, login: reports.search_terms_report(ads, cid, login, campaign_id, date_range, start_date,
                                                                     end_date, sort_by, min_cost_without_conversion, limit))

    @mcp.tool(annotations={"readOnlyHint": True})
    def pmax_channel_report(
        campaign_id: str | None = None,
        date_range: DateRange = "LAST_30_DAYS",
        start_date: str | None = None,
        end_date: str | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
    ) -> dict[str, Any]:
        """Performance Max split by channel (Google Search, Search partners, YouTube, Discover, Maps, Display,
        Gmail, ...) with cost, clicks, conversions and conversions per conversion action. A separate split by ad
        format is included; for Performance Max the API usually only reports OTHER there, so it gives no split
        such as local versus text ads."""
        return report("pmax_channel_report", customer_id, login_customer_id,
                      lambda cid, login: reports.pmax_channel_report(ads, cid, login, campaign_id, date_range,
                                                                     start_date, end_date))

    @mcp.tool(annotations={"readOnlyHint": True})
    def targeting_overview(
        campaign_id: str | None = None,
        include_paused: bool = False,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
    ) -> dict[str, Any]:
        """Per campaign: targeted and excluded locations, location option, languages, network settings and linked
        location assets, plus account-level location assets. Warns about campaigns without location targeting
        (they run worldwide) and campaigns targeting all languages."""
        return report("targeting_overview", customer_id, login_customer_id,
                      lambda cid, login: reports.targeting_overview(ads, cid, login, campaign_id, include_paused))

    @mcp.tool(annotations={"readOnlyHint": True})
    def recent_changes(limit: int = 20) -> dict[str, Any]:
        """Show the latest changes executed by this server (from logs/changes.jsonl), newest first,
        with time, profile, account, object, old and new value. Useful to report or undo by hand."""
        log.info("profile=%s customer=- tool=recent_changes", profile.name)
        return {"profile": profile.name, "customer_id": None,
                "changes": writer.recent_changes(max(1, min(int(limit), 200)))}

    # ---- write tools: dry run first, execute only with confirm + confirm_token ----

    def write(tool, customer_id, login_customer_id, confirm, confirm_token, build,
              ttl=TOKEN_TTL_SECONDS) -> dict[str, Any]:
        cid, login = resolve(customer_id, login_customer_id, tool)
        log.info("profile=%s customer=%s tool=%s %s", profile.name, cid, tool, "EXECUTE" if confirm else "dry run")
        try:
            result = writer.run(tool, cid, login, lambda: build(cid, login), bool(confirm), confirm_token, ttl)
        except (WriteRefused, AdsError) as e:
            raise fail(tool, cid, e) from e
        if login:
            result["login_customer_id"] = format_customer_id(login)
        log.info("profile=%s customer=%s tool=%s result=%s changes=%d", profile.name, cid, tool,
                 result["mode"], len(result.get("changes", [])))
        return result

    write_hints = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}

    @mcp.tool(annotations=write_hints)
    def set_status(
        object_type: Literal["keyword", "ad", "ad_group", "campaign", "asset_group"],
        ids: list[str],
        status: Status,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Pause or enable keywords, ads, ad groups, campaigns or Performance Max asset groups (max 50 per call).
        There is no delete. ID formats: keyword "adGroupId~criterionId", ad "adGroupId~adId", ad_group "adGroupId",
        campaign "campaignId", asset_group "assetGroupId". Campaigns only if the profile allows it
        (allow_campaign_status_change, default true).
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_status", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: writes.plan_set_status(ads, cid, login, object_type, ids, status,
                                                               profile.allow_campaign_status_change))

    @mcp.tool(annotations={**write_hints, "idempotentHint": False})
    def add_keywords(
        ad_group_id: str,
        keywords: list[KeywordIn],
        status: Status = "ENABLED",
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Add keywords to an ad group (max 50 per call). Keywords that already exist there are skipped.
        match_type is EXACT, PHRASE or BROAD; give the plain text without brackets or quotes.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        items = [k.model_dump() for k in keywords]
        return write("add_keywords", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: writes.plan_add_keywords(ads, cid, login, ad_group_id, items, status))

    @mcp.tool(annotations=write_hints)
    def set_device_bid_adjustment(
        campaign_id: str,
        device: Literal["MOBILE", "DESKTOP", "TABLET"],
        adjustment_percent: float,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Set a campaign's bid adjustment for one device, in percent: -20 means bids 20% lower,
        +15 means 15% higher, 0 means no adjustment, -100 excludes the device. Not for Performance Max.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_device_bid_adjustment", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: writes.plan_device_bid(ads, cid, login, campaign_id, device, adjustment_percent))

    @mcp.tool(annotations=write_hints)
    def set_ad_schedule_bid_adjustment(
        campaign_id: str,
        days: list[Day],
        start: str,
        end: str,
        adjustment_percent: float,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Set the bid adjustment for a time window in a campaign's ad schedule, e.g. days
        ["SATURDAY","SUNDAY"], start "08:00", end "12:30", adjustment_percent +20 (range -90 to +900,
        times in 15 minute steps, end may be "24:00"). If the window exists with these exact times its
        adjustment is changed, otherwise a new window is added (overlaps are refused). Times are in the
        account's time zone. Note: once a campaign has any window, ads only run inside its windows.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_ad_schedule_bid_adjustment", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: writes.plan_schedule_bid(ads, cid, login, campaign_id, list(days), start, end,
                                                                 adjustment_percent))

    @mcp.tool(annotations=write_hints)
    def set_campaign_budget(
        campaign_id: str,
        daily_amount: float,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Set the average daily budget of one campaign, in the account currency (e.g. 45.50).
        Limits from the profile: never above max_daily_budget, at most max_budget_change_percent per step
        (default 50%), no shared budgets. Use show_profile to see the limits.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_campaign_budget", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: writes.plan_campaign_budget(
                         ads, cid, login, campaign_id, daily_amount,
                         profile.max_daily_budget, profile.max_budget_change_percent))

    @mcp.tool(annotations=write_hints)
    def update_responsive_search_ad(
        ad_id: str,
        final_url: str | None = None,
        headlines: list[str] | None = None,
        descriptions: list[str] | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Change the final URL, headlines and/or descriptions of a responsive search ad.
        Only the fields you give are changed. headlines (3 to 15, max 30 characters each) and
        descriptions (2 to 4, max 90 characters) REPLACE the whole current list, so include the texts
        you want to keep. Pins are kept for unchanged texts. Google reviews the edited ad again.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("update_responsive_search_ad", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: writes.plan_update_rsa(ads, cid, login, ad_id, final_url, headlines, descriptions))

    # ---- negative keywords (priority 1 of the extension brief) ----

    @mcp.tool(annotations={**write_hints, "idempotentHint": False})
    def add_negative_keywords(
        level: NegLevel,
        keywords: list[NegativeKeywordIn],
        campaign_id: str | None = None,
        ad_group_id: str | None = None,
        shared_set_id: str | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Add negative keywords. level and the ID it needs:
        campaign -> campaign_id (Search, Shopping and Performance Max),
        ad_group -> ad_group_id (not Performance Max), shared_list -> shared_set_id of a negative keyword list,
        account -> no ID (the account-level list, must already exist in Google Ads).
        keywords: plain text plus match_type EXACT, PHRASE or BROAD. At most max_negative_keywords_per_call
        per call (profile, default 50). Negatives that already exist are skipped and named. The dry run warns
        when a negative would block active own keywords. Good keywords go through even if single ones fail.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        items = [k.model_dump() for k in keywords]
        return write("add_negative_keywords", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: negatives.plan_add_negative_keywords(
                         ads, cid, login, level, campaign_id, ad_group_id, shared_set_id, items,
                         profile.max_negative_keywords_per_call))

    @mcp.tool(annotations={**write_hints, "destructiveHint": True})
    def remove_negative_keywords(
        level: NegLevel,
        keywords: list[NegativeKeywordIn],
        campaign_id: str | None = None,
        ad_group_id: str | None = None,
        shared_set_id: str | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Remove negative keywords again, e.g. to undo a mistake. Same levels and IDs as add_negative_keywords;
        each keyword is matched by text and match_type. Only negative keywords can be removed, never normal ones.
        Keywords that are not negative at that level are named and skipped. At most max_negative_keywords_per_call.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        items = [k.model_dump() for k in keywords]
        return write("remove_negative_keywords", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: negatives.plan_remove_negative_keywords(
                         ads, cid, login, level, campaign_id, ad_group_id, shared_set_id, items,
                         profile.max_negative_keywords_per_call))

    @mcp.tool(annotations={**write_hints, "idempotentHint": False})
    def manage_negative_keyword_list(
        action: Literal["create", "attach", "detach"],
        name: str | None = None,
        shared_set_id: str | None = None,
        campaign_id: str | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Manage negative keyword lists (shared sets of type NEGATIVE_KEYWORDS).
        create -> name: creates an empty list. attach -> shared_set_id + campaign_id: the list's negatives apply
        to that campaign (also Performance Max). detach -> shared_set_id + campaign_id: removes only the link,
        the list itself stays. Lists are never deleted. Find list IDs with run_gaql on shared_set.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("manage_negative_keyword_list", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: negatives.plan_manage_negative_keyword_list(
                         ads, cid, login, action, name, shared_set_id, campaign_id))

    # ---- targeting and networks (priority 3 of the extension brief) ----

    @mcp.tool(annotations=write_hints)
    def set_location_targeting(
        campaign_id: str,
        add: list[str] | None = None,
        exclude: list[str] | None = None,
        remove: list[str] | None = None,
        country_code: str | None = None,
        location_type: Literal["PRESENCE", "PRESENCE_OR_INTEREST"] | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Change where a campaign shows ads (Search, Performance Max and others).
        add: target locations. exclude: exclude locations. remove: take a targeted or excluded location off again.
        Each entry is a geo target ID (e.g. "2620" for Portugal) or a name ("Lisbon"); names are looked up and the
        dry run shows the found name and type. Ambiguous names are refused with candidate IDs; country_code
        (e.g. "PT") narrows the lookup. location_type: PRESENCE (people in the location) or PRESENCE_OR_INTEREST
        (in or interested in it). The dry run warns loudly if the campaign would end up without any location
        (worldwide). Max 50 locations per call. Only if the profile allows it (allow_location_changes).
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_location_targeting", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: targeting.plan_location_targeting(
                         ads, cid, login, campaign_id, add or [], exclude or [], remove or [], country_code,
                         location_type, profile.allow_location_changes))

    @mcp.tool(annotations=write_hints)
    def set_language_targeting(
        campaign_id: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Add or remove target languages of a campaign. Each entry is a language constant ID (e.g. "1001"),
        a code ("de") or an English name ("German"). The dry run warns if the campaign would end up without
        any language (all languages).
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_language_targeting", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: targeting.plan_language_targeting(
                         ads, cid, login, campaign_id, add or [], remove or []))

    @mcp.tool(annotations=write_hints)
    def set_network_settings(
        campaign_id: str,
        search_partners: bool | None = None,
        display_network: bool | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Turn Google search partners and/or the Display network on or off for a Search campaign
        (campaign.network_settings). Only for Search campaigns; Performance Max has no such setting and is refused.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_network_settings", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: targeting.plan_network_settings(
                         ads, cid, login, campaign_id, search_partners, display_network))

    # ---- Performance Max (priority 4 of the extension brief) ----

    @mcp.tool(annotations=write_hints)
    def update_asset_group_text_assets(
        asset_group_id: str,
        headlines: list[str] | None = None,
        long_headlines: list[str] | None = None,
        descriptions: list[str] | None = None,
        business_name: str | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Change the text assets of a Performance Max asset group. Each given list REPLACES the current
        advertiser texts of that kind, so include the texts you want to keep. Google limits, checked before sending:
        headlines 3-15 (max 30 characters), long_headlines 1-5 (max 90), descriptions 2-5 (max 90),
        business_name max 25 (stored on the campaign when brand guidelines are on).
        Old texts are only unlinked, assets are never deleted; automatically created assets are not touched.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("update_asset_group_text_assets", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: pmax.plan_asset_group_text_assets(
                         ads, cid, login, asset_group_id, headlines, long_headlines, descriptions, business_name))

    @mcp.tool(annotations=write_hints)
    def set_search_themes(
        asset_group_id: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Add or remove search themes (signals) of a Performance Max asset group. Google allows 50 per asset
        group. Existing themes are skipped, unknown ones named. Good themes go through even if single ones fail.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_search_themes", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: pmax.plan_search_themes(ads, cid, login, asset_group_id, add or [], remove or []))

    @mcp.tool(annotations=write_hints)
    def set_brand_exclusions(
        campaign_id: str,
        shared_set_id: str,
        action: Literal["attach", "detach"],
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Attach a brand exclusion list (shared set of type BRANDS) to a Performance Max campaign, or detach it.
        Lists are created in Google Ads; find them with run_gaql on shared_set WHERE shared_set.type = BRANDS.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_brand_exclusions", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: pmax.plan_brand_exclusions(ads, cid, login, campaign_id, shared_set_id, action))

    # ---- bids (priority 5 of the extension brief) ----

    @mcp.tool(annotations=write_hints)
    def set_bidding_target(
        campaign_id: str,
        target_cpa: float | None = None,
        target_roas_percent: float | None = None,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Change the target of the campaign's current smart bidding strategy: target_cpa in account currency
        (Target CPA, or Maximize conversions with a target CPA) or target_roas_percent, e.g. 400 for 400%
        (Target ROAS, or Maximize conversion value with a target ROAS). Never switches strategies, refuses campaigns
        without such a target and portfolio (shared) strategies. Limits from the profile: max_target_change_percent
        per step (default 30) and max_target_cpa.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_bidding_target", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: bidding.plan_bidding_target(
                         ads, cid, login, campaign_id, target_cpa, target_roas_percent,
                         profile.max_target_change_percent, profile.max_target_cpa))

    @mcp.tool(annotations=write_hints)
    def set_keyword_bid(
        object_type: Literal["keyword", "ad_group"],
        ids: list[str],
        max_cpc: float,
        customer_id: str | None = None,
        login_customer_id: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Set the max CPC (in account currency) of keywords ("adGroupId~criterionId") or ad groups ("adGroupId"),
        max 50 per call. Only for campaigns with manual CPC bidding. Limit from the profile: max_cpc.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        return write("set_keyword_bid", customer_id, login_customer_id, confirm, confirm_token,
                     lambda cid, login: bidding.plan_keyword_bid(ads, cid, login, object_type, ids, max_cpc,
                                                                 profile.max_cpc))

    # ---- undo (priority 6 of the extension brief) ----

    @mcp.tool(annotations=write_hints)
    def revert_change(
        change_id: str,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Put the old value of one executed change back, using its change_id from recent_changes / the change log.
        Works for the changes of this profile and runs in the same account. Before reverting, the current value
        must still equal the logged new value; otherwise nothing happens and both values are shown. Changes that
        would need a delete (new keywords, new time windows, new lists) are refused with a reason.
        First call = dry run. Execute only after the user approved, with confirm=true and confirm_token.
        The confirm_token is valid for 15 minutes."""
        entry = writer.find_change(str(change_id).strip())
        if not entry:
            raise fail("revert_change", None, WriteRefused(f"No change with change_id '{change_id}' in the change log."))
        if entry.get("profile") != profile.name:
            raise fail("revert_change", None, WriteRefused(
                f"Change {change_id} was made with profile '{entry.get('profile')}', not '{profile.name}'. "
                "Revert it with that profile."))

        def build(cid, login):
            plan = revert.plan_revert(ads, cid, login, entry, profile)
            plan.log_extra = {"reverted_change_id": entry["change_id"],
                              "original_tool": entry.get("original_tool") or entry.get("tool")}
            return plan

        return write("revert_change", entry["customer_id"], None, confirm, confirm_token, build)

    return mcp


ACCOUNT_QUERY = """
SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone,
       customer.manager, customer.test_account, customer.status
FROM customer LIMIT 1
"""

CLIENTS_QUERY = """
SELECT customer_client.id, customer_client.descriptive_name, customer_client.manager,
       customer_client.status, customer_client.currency_code
FROM customer_client WHERE customer_client.level = 1
"""


def _describe_account(ads: Ads, profile: Profile, cid: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"customer_id": format_customer_id(cid), "writable": profile.is_write_allowed(cid)}
    try:
        rows, _ = ads.search(cid, ACCOUNT_QUERY, login_customer_id=cid, max_rows=1)
    except AdsError as e:
        entry["error"] = str(e)
        return entry
    if not rows:
        return entry
    row = rows[0]
    entry.update(
        name=row.get("customer.descriptive_name"),
        currency=row.get("customer.currency_code"),
        time_zone=row.get("customer.time_zone"),
        status=row.get("customer.status"),
        manager=row.get("customer.manager"),
        test_account=row.get("customer.test_account"),
    )
    if entry["manager"]:
        try:
            clients, _ = ads.search(cid, CLIENTS_QUERY, login_customer_id=cid, max_rows=MAX_ACCOUNTS)
        except AdsError as e:
            entry["clients_error"] = str(e)
        else:
            entry["clients"] = [
                {
                    "customer_id": format_customer_id(str(c["customer_client.id"])),
                    "name": c.get("customer_client.descriptive_name"),
                    "manager": c.get("customer_client.manager"),
                    "status": c.get("customer_client.status"),
                    "currency": c.get("customer_client.currency_code"),
                    "login_customer_id": format_customer_id(cid),
                    "writable": profile.is_write_allowed(str(c["customer_client.id"])),
                }
                for c in clients
            ]
    return entry


def main() -> None:
    setup_logging()
    try:
        profile = load_profile()
    except ConfigError as e:
        log.error("Startup aborted: %s", e)
        sys.exit(1)
    log.info("profile=%s customer=%s server starting", profile.name, profile.default_customer_id or "-")
    create_server(profile).run(show_banner=False)
