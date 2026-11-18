# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Write tools with guardrails.

Every write follows the same two calls:

1. Dry run (confirm=False, the default): read the current values, build the
   operations, let Google validate them (validate_only=True) and return
   old and new values plus a confirm_token. Nothing changes.
2. Execute (confirm=True + that confirm_token): the plan is built again from
   the live account. Only if it is identical to the dry run (same token) is it
   sent for real, and every change is written to logs/changes.jsonl.

Guardrails: writes only for customer IDs in the profile's allowlist, no
delete operations exist, no billing, users or account settings. Campaign
budgets only up to the profile's max_daily_budget and in limited steps.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from google.protobuf.field_mask_pb2 import FieldMask

from .ads import Ads
from .config import Profile, format_customer_id

MAX_ITEMS = 50
HEADLINE_LIMITS = (3, 15, 30)  # min count, max count, max characters
DESCRIPTION_LIMITS = (2, 4, 90)
DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
MINUTES = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}


class WriteRefused(Exception):
    """A write was blocked by a guardrail or by invalid input."""


@dataclass
class Change:
    object: str  # human readable, e.g. "keyword 'espresso beans' [PHRASE] in ad group 'Espresso'"
    field: str
    old: Any
    new: Any
    resource: str


@dataclass
class Plan:
    service: str
    method: str
    operations: list = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---- the engine ------------------------------------------------------------


class Writer:
    def __init__(self, profile: Profile, ads: Ads, log_folder: Path):
        self.profile = profile
        self.ads = ads
        self.changelog = log_folder / "changes.jsonl"
        self._secret = secrets.token_bytes(32)  # tokens are only valid while this server runs

    def run(
        self,
        tool: str,
        customer_id: str,
        login_customer_id: str | None,
        build: Callable[[], Plan],
        confirm: bool,
        confirm_token: str | None,
    ) -> dict[str, Any]:
        where = {"profile": self.profile.name, "customer_id": format_customer_id(customer_id)}
        if not self.profile.is_write_allowed(customer_id):
            raise WriteRefused(
                f"Account {format_customer_id(customer_id)} is not in allowed_customer_ids of profile "
                f"'{self.profile.name}'. Writing is refused. Reading still works."
            )

        plan = build()
        if not plan.changes:
            return {**where, "mode": "no_change", "message": "Everything already has the requested value.",
                    "warnings": plan.warnings}

        token = self._token(tool, customer_id, plan)
        changes = [_public(c) for c in plan.changes]

        if not confirm:
            self.ads.mutate(customer_id, plan.service, plan.method, plan.operations, login_customer_id, validate_only=True)
            return {
                **where,
                "mode": "dry_run",
                "message": "Validated by Google. Nothing was changed.",
                "changes": changes,
                "warnings": plan.warnings,
                "confirm_token": token,
                "next_step": (
                    "Show these changes to the user. Only after the user explicitly approves them, call this "
                    "tool again with exactly the same arguments plus confirm=true and this confirm_token."
                ),
            }

        if not confirm_token or not hmac.compare_digest(confirm_token, token):
            raise WriteRefused(
                "confirm_token missing or not matching. Either the arguments differ from the dry run, the "
                "account changed in the meantime, or the server was restarted. Run the dry run again."
            )

        self.ads.mutate(customer_id, plan.service, plan.method, plan.operations, login_customer_id, validate_only=False)
        self._log(tool, customer_id, plan.changes)
        return {**where, "mode": "executed", "message": "Changes are live and logged.", "changes": changes,
                "warnings": plan.warnings}

    def recent_changes(self, limit: int) -> list[dict[str, Any]]:
        if not self.changelog.is_file():
            return []
        lines = self.changelog.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in reversed(lines) if line.strip()]

    def _token(self, tool: str, customer_id: str, plan: Plan) -> str:
        payload = json.dumps(
            {"profile": self.profile.name, "customer": customer_id, "tool": tool, "changes": [asdict(c) for c in plan.changes]},
            sort_keys=True,
            default=str,
        )
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()[:20]

    def _log(self, tool: str, customer_id: str, changes: list[Change]) -> None:
        self.changelog.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with self.changelog.open("a", encoding="utf-8") as f:
            for change in changes:
                entry = {
                    "time": now,
                    "profile": self.profile.name,
                    "customer_id": format_customer_id(customer_id),
                    "tool": tool,
                    "object": change.object,
                    "field": change.field,
                    "old": change.old,
                    "new": change.new,
                    "resource": change.resource,
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _public(change: Change) -> dict[str, Any]:
    return {"object": change.object, "field": change.field, "old": change.old, "new": change.new}


def _mask(*paths: str) -> FieldMask:
    return FieldMask(paths=list(paths))


def _quote(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


# ---- 1. pause / enable -----------------------------------------------------

STATUS_TARGETS = {
    "keyword": {
        "id_pattern": r"\d+~\d+",
        "id_help": "adGroupId~criterionId, e.g. 123456~789012",
        "collection": "adGroupCriteria",
        "table": "ad_group_criterion",
        "fields": "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.type, "
                  "ad_group_criterion.negative",
        "service": "AdGroupCriterionService",
        "method": "mutate_ad_group_criteria",
        "operation": "AdGroupCriterionOperation",
        "enum": "AdGroupCriterionStatusEnum",
    },
    "ad": {
        "id_pattern": r"\d+~\d+",
        "id_help": "adGroupId~adId, e.g. 123456~789012",
        "collection": "adGroupAds",
        "table": "ad_group_ad",
        "fields": "ad_group_ad.ad.id, ad_group_ad.ad.type",
        "service": "AdGroupAdService",
        "method": "mutate_ad_group_ads",
        "operation": "AdGroupAdOperation",
        "enum": "AdGroupAdStatusEnum",
    },
    "ad_group": {
        "id_pattern": r"\d+",
        "id_help": "adGroupId, e.g. 123456",
        "collection": "adGroups",
        "table": "ad_group",
        "fields": "ad_group.id",
        "service": "AdGroupService",
        "method": "mutate_ad_groups",
        "operation": "AdGroupOperation",
        "enum": "AdGroupStatusEnum",
    },
}


def plan_set_status(ads: Ads, cid: str, login: str | None, object_type: str, ids: list[str], status: str) -> Plan:
    target = STATUS_TARGETS.get(object_type)
    if target is None:
        raise WriteRefused(f"object_type must be one of: {', '.join(STATUS_TARGETS)}.")
    if status not in ("PAUSED", "ENABLED"):
        raise WriteRefused("status must be PAUSED or ENABLED. Deleting is not supported.")
    ids = [str(i).strip() for i in ids]
    if not ids or len(ids) > MAX_ITEMS:
        raise WriteRefused(f"Give between 1 and {MAX_ITEMS} IDs.")
    bad = [i for i in ids if not re.fullmatch(target["id_pattern"], i)]
    if bad:
        raise WriteRefused(f"Invalid ID(s) {_quote(bad)}. Expected format: {target['id_help']}.")

    table = target["table"]
    names = [f"customers/{cid}/{target['collection']}/{i}" for i in dict.fromkeys(ids)]
    query = (
        f"SELECT {table}.resource_name, {table}.status, {target['fields']}, ad_group.name, ad_group.status, "
        "campaign.name, campaign.status "
        f"FROM {table} WHERE {table}.resource_name IN ({_quote(names)})"
    )
    rows, _ = ads.search(cid, query, login_customer_id=login, max_rows=MAX_ITEMS)
    found = {r[f"{table}.resource_name"]: r for r in rows}
    missing = [n.rsplit("/", 1)[-1] for n in names if n not in found]
    if missing:
        raise WriteRefused(f"Not found in account {format_customer_id(cid)}: {_quote(missing)}.")

    client = ads.client(login)
    plan = Plan(target["service"], target["method"])
    for name in names:
        row = found[name]
        old = row[f"{table}.status"]
        if "REMOVED" in (old, row.get("ad_group.status"), row.get("campaign.status")):
            raise WriteRefused(
                f"{_describe(object_type, row)} is removed or sits in a removed ad group or campaign. "
                "It cannot be changed."
            )
        if object_type == "keyword" and row.get("ad_group_criterion.type") != "KEYWORD":
            raise WriteRefused(f"{name.rsplit('/', 1)[-1]} is not a keyword.")
        if object_type == "keyword" and row.get("ad_group_criterion.negative"):
            raise WriteRefused(
                f"{_describe(object_type, row)} is a negative keyword. Google does not allow pausing or "
                "enabling negative keywords."
            )
        if old == status:
            plan.warnings.append(f"{_describe(object_type, row)} is already {status}, skipped.")
            continue
        op = client.get_type(target["operation"])
        op.update.resource_name = name
        op.update.status = getattr(client.enums, target["enum"])[status]
        client.copy_from(op.update_mask, _mask("status"))
        plan.operations.append(op)
        plan.changes.append(Change(_describe(object_type, row), "status", old, status, name))
    return plan


def _describe(object_type: str, row: dict) -> str:
    where = f"in ad group '{row.get('ad_group.name')}' (campaign '{row.get('campaign.name')}')"
    if object_type == "keyword":
        return f"keyword '{row.get('ad_group_criterion.keyword.text')}' [{row.get('ad_group_criterion.keyword.match_type')}] {where}"
    if object_type == "ad":
        return f"ad {row.get('ad_group_ad.ad.id')} ({row.get('ad_group_ad.ad.type')}) {where}"
    return f"ad group '{row.get('ad_group.name')}' (campaign '{row.get('campaign.name')}')"


# ---- 2. add keywords -------------------------------------------------------


def plan_add_keywords(ads: Ads, cid: str, login: str | None, ad_group_id: str, keywords: list[dict], status: str) -> Plan:
    if not re.fullmatch(r"\d+", str(ad_group_id)):
        raise WriteRefused("ad_group_id must be a number.")
    if status not in ("PAUSED", "ENABLED"):
        raise WriteRefused("status must be PAUSED or ENABLED.")
    if not keywords or len(keywords) > MAX_ITEMS:
        raise WriteRefused(f"Give between 1 and {MAX_ITEMS} keywords.")

    rows, _ = ads.search(
        cid,
        "SELECT ad_group.resource_name, ad_group.name, ad_group.status, campaign.name, campaign.status "
        f"FROM ad_group WHERE ad_group.id = {ad_group_id}",
        login_customer_id=login,
        max_rows=1,
    )
    if not rows or "REMOVED" in (rows[0]["ad_group.status"], rows[0].get("campaign.status")):
        raise WriteRefused(f"Ad group {ad_group_id} not found, or it or its campaign is removed, in account {format_customer_id(cid)}.")
    group = rows[0]

    existing_rows, _ = ads.search(
        cid,
        "SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type FROM ad_group_criterion "
        f"WHERE ad_group.id = {ad_group_id} AND ad_group_criterion.type = KEYWORD "
        "AND ad_group_criterion.negative = FALSE AND ad_group_criterion.status != 'REMOVED'",
        login_customer_id=login,
        max_rows=10000,
    )
    existing = {(r["ad_group_criterion.keyword.text"].lower(), r["ad_group_criterion.keyword.match_type"]) for r in existing_rows}

    client = ads.client(login)
    plan = Plan("AdGroupCriterionService", "mutate_ad_group_criteria")
    seen: set[tuple[str, str]] = set()
    for kw in keywords:
        text = " ".join(str(kw.get("text", "")).split())
        match = str(kw.get("match_type", "PHRASE")).upper()
        if not text:
            raise WriteRefused("Empty keyword text.")
        if match not in ("EXACT", "PHRASE", "BROAD"):
            raise WriteRefused(f"match_type for '{text}' must be EXACT, PHRASE or BROAD.")
        key = (text.lower(), match)
        if key in existing or key in seen:
            plan.warnings.append(f"Keyword '{text}' [{match}] already exists in this ad group, skipped.")
            continue
        seen.add(key)
        op = client.get_type("AdGroupCriterionOperation")
        op.create.ad_group = group["ad_group.resource_name"]
        op.create.status = client.enums.AdGroupCriterionStatusEnum[status]
        op.create.keyword.text = text
        op.create.keyword.match_type = client.enums.KeywordMatchTypeEnum[match]
        plan.operations.append(op)
        plan.changes.append(
            Change(
                f"keyword '{text}' [{match}] in ad group '{group['ad_group.name']}' (campaign '{group['campaign.name']}')",
                "keyword",
                None,
                f"added, status {status}",
                group["ad_group.resource_name"],
            )
        )
    return plan


# ---- 3. bid adjustments: device and ad schedule ----------------------------


def percent_to_modifier(percent: float) -> float:
    return round(1 + float(percent) / 100, 4)


def modifier_to_text(modifier: float | None) -> str:
    if modifier is None:
        return "no adjustment (0%)"
    if modifier == 0:
        return "-100% (excluded)"
    pct = round((modifier - 1) * 100, 1)
    pct = int(pct) if pct == int(pct) else pct
    return f"{'+' if pct > 0 else ''}{pct}%"


def _campaign(ads: Ads, cid: str, login: str | None, campaign_id: str) -> dict:
    if not re.fullmatch(r"\d+", str(campaign_id)):
        raise WriteRefused("campaign_id must be a number.")
    rows, _ = ads.search(
        cid,
        "SELECT campaign.resource_name, campaign.name, campaign.status, campaign.advertising_channel_type "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
        login_customer_id=login,
        max_rows=1,
    )
    if not rows or rows[0]["campaign.status"] == "REMOVED":
        raise WriteRefused(f"Campaign {campaign_id} not found (or removed) in account {format_customer_id(cid)}.")
    if rows[0]["campaign.advertising_channel_type"] == "PERFORMANCE_MAX":
        raise WriteRefused("Performance Max campaigns do not support bid adjustments.")
    return rows[0]


def plan_device_bid(ads: Ads, cid: str, login: str | None, campaign_id: str, device: str, percent: float) -> Plan:
    device = device.upper()
    if device not in ("MOBILE", "DESKTOP", "TABLET"):
        raise WriteRefused("device must be MOBILE, DESKTOP or TABLET.")
    if not -100 <= float(percent) <= 900:
        raise WriteRefused("adjustment_percent must be between -100 (exclude device) and +900.")
    campaign = _campaign(ads, cid, login, campaign_id)
    rows, _ = ads.search(
        cid,
        "SELECT campaign_criterion.resource_name, campaign_criterion.bid_modifier FROM campaign_criterion "
        f"WHERE campaign.id = {campaign_id} AND campaign_criterion.type = DEVICE "
        f"AND campaign_criterion.device.type = {device}",
        login_customer_id=login,
        max_rows=1,
    )
    old = rows[0]["campaign_criterion.bid_modifier"] if rows else None
    new = percent_to_modifier(percent)
    plan = Plan("CampaignCriterionService", "mutate_campaign_criteria")
    if old is not None and round(old, 4) == new or old is None and new == 1:
        return plan

    client = ads.client(login)
    op = client.get_type("CampaignCriterionOperation")
    if rows:
        resource = rows[0]["campaign_criterion.resource_name"]
        op.update.resource_name = resource
        op.update.bid_modifier = new
        client.copy_from(op.update_mask, _mask("bid_modifier"))
    else:
        resource = campaign["campaign.resource_name"]
        op.create.campaign = resource
        op.create.device.type_ = client.enums.DeviceEnum[device]
        op.create.bid_modifier = new
    plan.operations.append(op)
    plan.changes.append(
        Change(f"{device} bid adjustment in campaign '{campaign['campaign.name']}'", "bid_modifier",
               modifier_to_text(old), modifier_to_text(new), resource)
    )
    return plan


def parse_time(value: str, is_end: bool) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        raise WriteRefused(f"Time '{value}' must look like 08:00.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if minute not in MINUTES:
        raise WriteRefused(f"Time '{value}': minutes must be 00, 15, 30 or 45.")
    if hour > 24 or hour == 24 and (minute != 0 or not is_end):
        raise WriteRefused(f"Time '{value}' is out of range (00:00 to 24:00).")
    return hour, minute


def plan_schedule_bid(
    ads: Ads, cid: str, login: str | None, campaign_id: str, days: list[str], start: str, end: str, percent: float
) -> Plan:
    days = [d.upper() for d in dict.fromkeys(days)]
    bad = [d for d in days if d not in DAYS]
    if not days or bad:
        raise WriteRefused(f"days must be a list of: {', '.join(DAYS)}.")
    start_t, end_t = parse_time(start, False), parse_time(end, True)
    if start_t >= end_t:
        raise WriteRefused("start must be before end.")
    if not -90 <= float(percent) <= 900:
        raise WriteRefused("adjustment_percent must be between -90 and +900 for ad schedules.")

    campaign = _campaign(ads, cid, login, campaign_id)
    rows, _ = ads.search(
        cid,
        "SELECT campaign_criterion.resource_name, campaign_criterion.bid_modifier, campaign_criterion.ad_schedule.day_of_week, "
        "campaign_criterion.ad_schedule.start_hour, campaign_criterion.ad_schedule.start_minute, "
        "campaign_criterion.ad_schedule.end_hour, campaign_criterion.ad_schedule.end_minute "
        f"FROM campaign_criterion WHERE campaign.id = {campaign_id} AND campaign_criterion.type = AD_SCHEDULE",
        login_customer_id=login,
        max_rows=500,
    )
    minute_value = {v: k for k, v in MINUTES.items()}

    def window(r: dict) -> tuple[str, tuple[int, int], tuple[int, int]]:
        p = "campaign_criterion.ad_schedule."
        return (r[p + "day_of_week"], (r[p + "start_hour"], minute_value[r[p + "start_minute"]]),
                (r[p + "end_hour"], minute_value[r[p + "end_minute"]]))

    windows = [(window(r), r) for r in rows]
    new = percent_to_modifier(percent)
    client = ads.client(login)
    plan = Plan("CampaignCriterionService", "mutate_campaign_criteria")
    if not rows:
        plan.warnings.append(
            f"Campaign '{campaign['campaign.name']}' has no ad schedule yet. Adding a time window means ads "
            "ONLY run in the scheduled windows from then on."
        )
    label = f"{start}-{end}"
    for day in days:
        exact = next((r for (d, s, e), r in windows if d == day and s == start_t and e == end_t), None)
        if exact:
            old = exact["campaign_criterion.bid_modifier"]
            if old is not None and round(old, 4) == new or old is None and new == 1:
                plan.warnings.append(f"{day} {label} already has {modifier_to_text(old)}, skipped.")
                continue
            op = client.get_type("CampaignCriterionOperation")
            op.update.resource_name = exact["campaign_criterion.resource_name"]
            op.update.bid_modifier = new
            client.copy_from(op.update_mask, _mask("bid_modifier"))
            resource = exact["campaign_criterion.resource_name"]
        else:
            overlaps = [f"{s[0]:02d}:{s[1]:02d}-{e[0]:02d}:{e[1]:02d}" for (d, s, e), _ in windows
                        if d == day and s < end_t and start_t < e]
            if overlaps:
                raise WriteRefused(
                    f"{day} {label} overlaps existing window(s) {', '.join(overlaps)}. ads-pilot only changes the "
                    "adjustment of an existing window (use its exact times) or adds non-overlapping windows. "
                    "Splitting or removing windows has to be done in Google Ads."
                )
            old = None
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = campaign["campaign.resource_name"]
            op.create.bid_modifier = new
            sched = op.create.ad_schedule
            sched.day_of_week = client.enums.DayOfWeekEnum[day]
            sched.start_hour, sched.end_hour = start_t[0], end_t[0]
            sched.start_minute = client.enums.MinuteOfHourEnum[MINUTES[start_t[1]]]
            sched.end_minute = client.enums.MinuteOfHourEnum[MINUTES[end_t[1]]]
            resource = campaign["campaign.resource_name"]
        plan.operations.append(op)
        plan.changes.append(
            Change(f"ad schedule {day} {label} in campaign '{campaign['campaign.name']}'", "bid_modifier",
                   "no time window yet" if not exact else modifier_to_text(old), modifier_to_text(new), resource)
        )
    return plan


# ---- 4. campaign budget ---------------------------------------------------


def plan_campaign_budget(
    ads: Ads,
    cid: str,
    login: str | None,
    campaign_id: str,
    daily_amount: float,
    max_daily_budget: float | None,
    max_change_percent: float,
) -> Plan:
    """Change the daily budget of one campaign, within the profile's limits."""
    if max_daily_budget is None:
        raise WriteRefused(
            "Budget changes are off for this profile. Add max_daily_budget (highest daily budget per "
            "campaign, in account currency) to the profile file to allow them."
        )
    if not re.fullmatch(r"\d+", str(campaign_id)):
        raise WriteRefused("campaign_id must be a number.")
    new = round(float(daily_amount), 2)
    if new <= 0:
        raise WriteRefused("daily_amount must be greater than 0. To stop spending, pause the campaign instead.")
    if new > max_daily_budget:
        raise WriteRefused(
            f"{new:.2f} is above this profile's max_daily_budget of {max_daily_budget:.2f}. "
            "Raise the limit in the profile file if this is intended."
        )

    rows, _ = ads.search(
        cid,
        "SELECT campaign.name, campaign.status, campaign_budget.resource_name, campaign_budget.amount_micros, "
        "campaign_budget.explicitly_shared, campaign_budget.reference_count, customer.currency_code "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
        login_customer_id=login,
        max_rows=1,
    )
    if not rows or rows[0]["campaign.status"] == "REMOVED":
        raise WriteRefused(f"Campaign {campaign_id} not found (or removed) in account {format_customer_id(cid)}.")
    row = rows[0]
    if row.get("campaign_budget.explicitly_shared") or (row.get("campaign_budget.reference_count") or 1) > 1:
        raise WriteRefused(
            f"Campaign '{row['campaign.name']}' uses a shared budget. Changing it would affect other campaigns too, "
            "so ads-pilot does not change shared budgets. Do this in Google Ads."
        )

    currency = row.get("customer.currency_code") or ""
    old = round((row["campaign_budget.amount_micros"] or 0) / 1_000_000, 2)
    plan = Plan("CampaignBudgetService", "mutate_campaign_budgets")
    if old == new:
        return plan
    if old > 0:
        change = (new - old) / old * 100
        if abs(change) > max_change_percent:
            raise WriteRefused(
                f"From {old:.2f} to {new:.2f} {currency} is a change of {change:+.0f}%. This profile allows at most "
                f"{max_change_percent:.0f}% per step (max_budget_change_percent). Change it in smaller steps."
            )

    client = ads.client(login)
    op = client.get_type("CampaignBudgetOperation")
    op.update.resource_name = row["campaign_budget.resource_name"]
    op.update.amount_micros = int(round(new * 100)) * 10_000  # Google wants whole cents
    client.copy_from(op.update_mask, _mask("amount_micros"))
    plan.operations.append(op)
    plan.changes.append(
        Change(f"daily budget of campaign '{row['campaign.name']}'", "daily_budget",
               f"{old:.2f} {currency}".strip(), f"{new:.2f} {currency}".strip(), row["campaign_budget.resource_name"])
    )
    if new > old:
        plan.warnings.append("A higher budget means Google may spend more from today on.")
    return plan


# ---- 5. responsive search ad: final URL, headlines, descriptions -----------


def plan_update_rsa(
    ads: Ads,
    cid: str,
    login: str | None,
    ad_id: str,
    final_url: str | None,
    headlines: list[str] | None,
    descriptions: list[str] | None,
) -> Plan:
    if not re.fullmatch(r"\d+", str(ad_id)):
        raise WriteRefused("ad_id must be a number.")
    if final_url is None and headlines is None and descriptions is None:
        raise WriteRefused("Give at least one of final_url, headlines, descriptions.")
    if final_url is not None and not re.match(r"https?://\S+$", final_url.strip()):
        raise WriteRefused("final_url must start with https:// (or http://).")
    if headlines is not None:
        _check_texts("headlines", headlines, HEADLINE_LIMITS)
    if descriptions is not None:
        _check_texts("descriptions", descriptions, DESCRIPTION_LIMITS)

    rows, _ = ads.search(
        cid,
        "SELECT ad_group_ad.status, ad_group_ad.ad.type, ad_group_ad.ad.final_urls, "
        "ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions, "
        f"ad_group.name, ad_group.status, campaign.name, campaign.status FROM ad_group_ad WHERE ad_group_ad.ad.id = {ad_id}",
        login_customer_id=login,
        max_rows=1,
    )
    if not rows or "REMOVED" in (rows[0]["ad_group_ad.status"], rows[0].get("ad_group.status"), rows[0].get("campaign.status")):
        raise WriteRefused(f"Ad {ad_id} not found, or it, its ad group or its campaign is removed, in account {format_customer_id(cid)}.")
    row = rows[0]
    if row["ad_group_ad.ad.type"] != "RESPONSIVE_SEARCH_AD":
        raise WriteRefused(f"Ad {ad_id} is a {row['ad_group_ad.ad.type']}, not a responsive search ad.")

    client = ads.client(login)
    resource = f"customers/{cid}/ads/{ad_id}"
    label = f"responsive search ad {ad_id} in ad group '{row['ad_group.name']}' (campaign '{row['campaign.name']}')"
    op = client.get_type("AdOperation")
    op.update.resource_name = resource
    paths: list[str] = []
    plan = Plan("AdService", "mutate_ads")

    if final_url is not None:
        old_urls = list(row["ad_group_ad.ad.final_urls"] or [])
        if old_urls != [final_url.strip()]:
            op.update.final_urls.append(final_url.strip())
            paths.append("final_urls")
            plan.changes.append(Change(label, "final_url", old_urls, [final_url.strip()], resource))

    for name, new_texts in (("headlines", headlines), ("descriptions", descriptions)):
        if new_texts is None:
            continue
        old_assets = row[f"ad_group_ad.ad.responsive_search_ad.{name}"] or []
        pins = {a["text"]: a.get("pinned_field") for a in old_assets if a.get("pinned_field") not in (None, "UNSPECIFIED")}
        new_texts = [t.strip() for t in new_texts]
        old_view = [_asset_text(a) for a in old_assets]
        new_view = [t + (f" [pinned {pins[t]}]" if t in pins else "") for t in new_texts]
        if old_view == new_view:
            continue
        dropped = [t for t in pins if t not in new_texts]
        if dropped:
            plan.warnings.append(f"Pinned {name} removed: {_quote(dropped)}.")
        target = getattr(op.update.responsive_search_ad, name)
        for text in new_texts:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            if text in pins:
                asset.pinned_field = client.enums.ServedAssetFieldTypeEnum[pins[text]]
            target.append(asset)
        paths.append(f"responsive_search_ad.{name}")
        plan.changes.append(Change(label, name, old_view, new_view, resource))

    if paths:
        client.copy_from(op.update_mask, _mask(*paths))
        plan.operations.append(op)
        plan.warnings.append("Google reviews the edited ad again before it shows with the new content.")
    return plan


def _asset_text(asset: dict) -> str:
    pin = asset.get("pinned_field")
    return asset["text"] + (f" [pinned {pin}]" if pin not in (None, "UNSPECIFIED") else "")


def _check_texts(name: str, texts: list[str], limits: tuple[int, int, int]) -> None:
    low, high, chars = limits
    if not low <= len(texts) <= high:
        raise WriteRefused(f"{name}: give between {low} and {high} texts (got {len(texts)}). The list replaces all current {name}.")
    for t in texts:
        # Texts with {KeyWord:...} or other insertions are checked by Google itself.
        if "{" not in t and len(t.strip()) > chars:
            raise WriteRefused(f"{name}: '{t}' has {len(t.strip())} characters, the limit is {chars}.")
        if not t.strip():
            raise WriteRefused(f"{name}: empty text.")
    if len({t.strip().lower() for t in texts}) != len(texts):
        raise WriteRefused(f"{name}: duplicate texts.")
