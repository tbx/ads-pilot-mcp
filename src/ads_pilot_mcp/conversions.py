# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Conversion actions: list them, change their status, change their settings.

Conversion actions decide what smart bidding optimizes for, so the write tools need the access
level "full" and check more than other tools:
- The API can set ENABLED and REMOVED, not HIDDEN (Google rejects it: BLOCKED_VALUE). Hiding stays
  a Google Ads UI action; making an action secondary (primary_for_goal=false) is the API way to stop
  it from steering bids.
- REMOVED is final (Google cannot enable a removed conversion action again). Removing is only
  allowed when the action had no conversions in the last conversion_remove_lookback_days days.
- Universal Analytics actions cannot be changed through the API at all (MUTATE_NOT_ALLOWED).
  For GA4 imports, counting type and click-through window come from GA4 and are immutable.
- If a change leaves a conversion goal (category + origin) without any enabled primary action, the
  campaigns bidding on that goal are named. When one of them uses smart bidding, the change is
  blocked unless allow_goal_loss=true.
- Google-owned actions (origin GOOGLE_HOSTED, e.g. local actions, store visits) and actions owned
  by a manager account are refused.
ads-pilot never creates conversion actions and never changes campaign goals.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .ads import Ads
from .config import Profile, format_customer_id
from .writes import Change, Plan, WriteRefused, _mask, _quote

TOOLS = ("set_conversion_action_status", "set_conversion_action_settings")
STATUSES = ("ENABLED", "REMOVED")  # HIDDEN can be read, but the API does not allow setting it
COUNTING_TYPES = ("ONE_PER_CLICK", "MANY_PER_CLICK")
CALL_TYPES = ("AD_CALL", "WEBSITE_CALL")
SMART_BIDDING = ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "TARGET_CPA", "TARGET_ROAS")
GOOGLE_OWNED_TYPES = ("GOOGLE_HOSTED", "STORE_VISITS", "STORE_SALES", "SMART_CAMPAIGN_AD_CLICKS_TO_CALL",
                      "SMART_CAMPAIGN_MAP_CLICKS_TO_CALL", "SMART_CAMPAIGN_MAP_DIRECTIONS",
                      "SMART_CAMPAIGN_TRACKED_CALLS", "ANDROID_APP_PRE_REGISTRATION", "ANDROID_INSTALLS_ALL_OTHER_APPS")
UA_TYPES = ("UNIVERSAL_ANALYTICS_GOAL", "UNIVERSAL_ANALYTICS_TRANSACTION")
GA4_TYPES = ("GOOGLE_ANALYTICS_4_CUSTOM", "GOOGLE_ANALYTICS_4_PURCHASE")
IMMUTABLE_SETTINGS = {t: ("counting_type", "click_through_lookback_window_days") for t in GA4_TYPES}

# What the API lets ads-pilot do per type. Found with validate_only dry runs against a real account
# (Google Ads API v26, November 2026); see docs/conversions.md.
TYPE_SUPPORT = {  # "tested" = validate_only against a real account; "docs" = from the API reference only
    "WEBPAGE": "enable, remove, all settings (tested)",
    "GOOGLE_ANALYTICS_4_CUSTOM / _PURCHASE": "enable, remove, primary, value, view-through window; NOT counting type "
                                            "or click-through window, they come from GA4 (tested)",
    "UNIVERSAL_ANALYTICS_GOAL / _TRANSACTION": "nothing, the API refuses every change: remove them in Google Ads (tested)",
    "AD_CALL, WEBSITE_CALL": "enable, remove, settings incl. phone_call_duration_seconds (docs, not tested)",
    "CLICK_TO_CALL, UPLOAD_CLICKS, WEBPAGE_CODELESS": "enable, remove, settings (docs, not tested)",
    "GOOGLE_HOSTED and other Google-owned types": "nothing (refused by ads-pilot)",
    "any type": "HIDDEN cannot be set through the API (tested); hide in Google Ads or make the action secondary",
}

FIELDS = ("conversion_action.resource_name", "conversion_action.id", "conversion_action.name",
          "conversion_action.type", "conversion_action.category", "conversion_action.origin",
          "conversion_action.status", "conversion_action.primary_for_goal",
          "conversion_action.include_in_conversions_metric", "conversion_action.counting_type",
          "conversion_action.value_settings.default_value", "conversion_action.value_settings.always_use_default_value",
          "conversion_action.click_through_lookback_window_days", "conversion_action.view_through_lookback_window_days",
          "conversion_action.phone_call_duration_seconds", "conversion_action.owner_customer",
          "conversion_action.google_analytics_4_settings.property_name",
          "conversion_action.google_analytics_4_settings.event_name")


def _short(row: dict[str, Any]) -> dict[str, Any]:
    return {k.replace("conversion_action.", ""): v for k, v in row.items() if k.startswith("conversion_action.")}


def mutable_reason(row: dict[str, Any], cid: str) -> str | None:
    """None if ads-pilot can change this action, else the reason why not."""
    if row["conversion_action.status"] == "REMOVED":
        return "removed (final)"
    if row["conversion_action.origin"] == "GOOGLE_HOSTED" or row["conversion_action.type"] in GOOGLE_OWNED_TYPES:
        return "Google-owned action, managed by Google"
    if row["conversion_action.type"] in UA_TYPES:
        return "Universal Analytics action: the API allows no changes, remove it in Google Ads (Goals > Conversions)"
    owner = row.get("conversion_action.owner_customer") or ""
    if owner and owner != f"customers/{cid}":
        return f"owned by account {format_customer_id(owner.rsplit('/', 1)[-1])} (manager), change it there"
    return None


def conversion_counts(ads: Ads, cid: str, login: str | None, days: int) -> dict[str, dict[str, float]]:
    """resource name -> {'conversions', 'all_conversions'} over the last `days` days."""
    today = dt.date.today()
    rows, _ = ads.search(cid, "SELECT segments.conversion_action, metrics.conversions, metrics.all_conversions FROM customer "
                         f"WHERE segments.date BETWEEN '{today - dt.timedelta(days=days)}' AND '{today}'",
                         login_customer_id=login, max_rows=1000)
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        entry = out.setdefault(r["segments.conversion_action"], {"conversions": 0.0, "all_conversions": 0.0})
        entry["conversions"] += float(r.get("metrics.conversions") or 0)
        entry["all_conversions"] += float(r.get("metrics.all_conversions") or 0)
    return out


def list_conversion_actions(ads: Ads, cid: str, login: str | None, include_removed: bool, lookback_days: int) -> dict:
    lookback_days = max(1, min(int(lookback_days), 365))
    where = "" if include_removed else " WHERE conversion_action.status != 'REMOVED'"
    rows, truncated = ads.search(cid, f"SELECT {', '.join(FIELDS)} FROM conversion_action{where} "
                                 "ORDER BY conversion_action.status, conversion_action.name",
                                 login_customer_id=login, max_rows=500)
    counts = conversion_counts(ads, cid, login, lookback_days)
    actions, summary, primary_goals = [], {"ENABLED": 0, "HIDDEN": 0, "REMOVED": 0}, set()
    for r in rows:
        item = _short(r)
        item.pop("resource_name", None)
        if item["type"] not in ("GOOGLE_ANALYTICS_4_CUSTOM", "GOOGLE_ANALYTICS_4_PURCHASE"):
            item.pop("google_analytics_4_settings.property_name", None)
            item.pop("google_analytics_4_settings.event_name", None)
        if item["type"] not in CALL_TYPES:
            item.pop("phone_call_duration_seconds", None)
        stats = counts.get(r["conversion_action.resource_name"], {})
        item["conversions"] = round(stats.get("conversions", 0.0), 2)
        item["all_conversions"] = round(stats.get("all_conversions", 0.0), 2)
        reason = mutable_reason(r, cid)
        item["mutable"] = reason is None
        if reason:
            item["not_mutable_because"] = reason
        summary[item["status"]] = summary.get(item["status"], 0) + 1
        if item["status"] == "ENABLED" and item["primary_for_goal"]:
            primary_goals.add(f"{item['category']} ({item['origin']})")
        actions.append(item)
    return {"lookback_days": lookback_days, "summary": {**summary, "goals_with_primary_actions": sorted(primary_goals)},
            "conversion_actions": actions, "truncated": truncated}


def _load(ads: Ads, cid: str, login: str | None, ids: list[str], limit: int) -> list[dict[str, Any]]:
    ids = [str(i).strip() for i in ids]
    if not ids or len(ids) > limit:
        raise WriteRefused(f"Give between 1 and {limit} conversion action IDs (max_conversion_actions_per_call).")
    bad = [i for i in ids if not re.fullmatch(r"\d+", i)]
    if bad:
        raise WriteRefused(f"Invalid ID(s) {_quote(bad)}. Conversion action IDs are numbers.")
    names = [f"customers/{cid}/conversionActions/{i}" for i in dict.fromkeys(ids)]
    rows, _ = ads.search(cid, f"SELECT {', '.join(FIELDS)} FROM conversion_action "
                         f"WHERE conversion_action.resource_name IN ({_quote(names)})", login_customer_id=login,
                         max_rows=len(names))
    found = {r["conversion_action.resource_name"]: r for r in rows}
    missing = [n.rsplit("/", 1)[-1] for n in names if n not in found]
    if missing:
        raise WriteRefused(f"Conversion action(s) not found in account {format_customer_id(cid)}: {_quote(missing)}.")
    return [found[n] for n in names]


def _label(row: dict[str, Any]) -> str:
    return f"conversion action '{row['conversion_action.name']}' ({row['conversion_action.type']})"


def goal_check(ads: Ads, cid: str, login: str | None, leaving: list[dict[str, Any]], allow_goal_loss: bool,
               plan: Plan) -> None:
    """Warn, or block, when actions that stop counting as enabled primary leave a goal empty."""
    goals = {(r["conversion_action.category"], r["conversion_action.origin"]) for r in leaving
             if r["conversion_action.status"] == "ENABLED" and r["conversion_action.primary_for_goal"]}
    if not goals:
        return
    leaving_names = {r["conversion_action.resource_name"] for r in leaving}
    rows, _ = ads.search(cid, "SELECT conversion_action.resource_name, conversion_action.category, conversion_action.origin "
                         "FROM conversion_action WHERE conversion_action.status = 'ENABLED' "
                         "AND conversion_action.primary_for_goal = TRUE", login_customer_id=login, max_rows=500)
    remaining = {(r["conversion_action.category"], r["conversion_action.origin"]) for r in rows
                 if r["conversion_action.resource_name"] not in leaving_names}
    empty = sorted(goals - remaining)
    if not empty:
        return
    campaigns, _ = ads.search(cid, "SELECT campaign.id, campaign.name, campaign.status, campaign.bidding_strategy_type, "
                              "campaign_conversion_goal.category, campaign_conversion_goal.origin FROM campaign_conversion_goal "
                              "WHERE campaign_conversion_goal.biddable = TRUE AND campaign.status != 'REMOVED'",
                              login_customer_id=login, max_rows=1000)
    blocked = []
    for category, origin in empty:
        hit = [c for c in campaigns if (c["campaign_conversion_goal.category"], c["campaign_conversion_goal.origin"])
               == (category, origin)]
        smart = [c for c in hit if c["campaign.bidding_strategy_type"] in SMART_BIDDING]
        names = ", ".join(f"'{c['campaign.name']}' ({c['campaign.bidding_strategy_type']})" for c in hit) or "none"
        plan.warnings.append(f"Goal {category} ({origin}) would have no enabled primary conversion action left. "
                             f"Campaigns bidding on it: {names}.")
        if smart:
            blocked.append(f"{category} ({origin}): " + ", ".join(f"'{c['campaign.name']}'" for c in smart))
    if blocked and not allow_goal_loss:
        raise WriteRefused("Blocked: smart bidding campaigns would lose their last primary conversion action for "
                           + "; ".join(blocked) + ". They would bid without that goal. Add another primary action first, "
                           "or repeat with allow_goal_loss=true if this is really intended.")


def plan_conversion_status(ads: Ads, cid: str, login: str | None, ids: list[str], status: str,
                           allow_goal_loss: bool, profile: Profile) -> Plan:
    status = str(status or "").strip().upper()
    if status == "HIDDEN":
        raise WriteRefused("Google does not allow hiding conversion actions through the API. Hide them in Google Ads, "
                           "or make them secondary with set_conversion_action_settings(primary_for_goal=false) so they "
                           "no longer steer bidding.")
    if status not in STATUSES:
        raise WriteRefused(f"status must be one of: {', '.join(STATUSES)}.")
    rows = _load(ads, cid, login, ids, profile.max_conversion_actions_per_call)
    for r in rows:
        reason = mutable_reason(r, cid)
        if reason:
            raise WriteRefused(f"{_label(r)} cannot be changed: {reason}.")

    plan = Plan("ConversionActionService", "mutate_conversion_actions", partial_failure=True)
    todo = [r for r in rows if r["conversion_action.status"] != status]
    for r in rows:
        if r not in todo:
            plan.warnings.append(f"{_label(r)} already is {status}, skipped.")
    if not todo:
        return plan

    if status == "REMOVED":
        days = profile.conversion_remove_lookback_days
        counts = conversion_counts(ads, cid, login, days)
        with_data = [(r, counts[r["conversion_action.resource_name"]]["all_conversions"]) for r in todo
                     if counts.get(r["conversion_action.resource_name"], {}).get("all_conversions", 0) > 0]
        if with_data:
            listed = "; ".join(f"'{r['conversion_action.name']}' ({n:g})" for r, n in with_data)
            raise WriteRefused(f"Not removed: these actions had conversions in the last {days} days: {listed}. "
                               "Removing is final and only allowed without data. Make them secondary instead "
                               "(set_conversion_action_settings primary_for_goal=false) or hide them in Google Ads.")
        plan.warnings.append("Removing is final: Google cannot enable a removed conversion action again, and "
                             "revert_change cannot undo it. Past conversions stay in the reports.")
    if status == "REMOVED":
        goal_check(ads, cid, login, todo, allow_goal_loss, plan)

    client = ads.client(login)
    for r in todo:
        op = client.get_type("ConversionActionOperation")
        if status == "REMOVED":
            op.remove = r["conversion_action.resource_name"]
        else:
            op.update.resource_name = r["conversion_action.resource_name"]
            op.update.status = client.enums.ConversionActionStatusEnum[status]
            client.copy_from(op.update_mask, _mask("status"))
        plan.operations.append(op)
        plan.changes.append(Change(_label(r), "status", r["conversion_action.status"], status,
                                   r["conversion_action.resource_name"], object_type="conversion_action",
                                   object_id=str(r["conversion_action.id"]), name=r["conversion_action.name"]))
    return plan


SETTINGS = {  # parameter -> (field path in the API, column in our query)
    "primary_for_goal": ("primary_for_goal", "conversion_action.primary_for_goal"),
    "default_value": ("value_settings.default_value", "conversion_action.value_settings.default_value"),
    "always_use_default_value": ("value_settings.always_use_default_value",
                                 "conversion_action.value_settings.always_use_default_value"),
    "counting_type": ("counting_type", "conversion_action.counting_type"),
    "click_through_lookback_window_days": ("click_through_lookback_window_days",
                                           "conversion_action.click_through_lookback_window_days"),
    "view_through_lookback_window_days": ("view_through_lookback_window_days",
                                          "conversion_action.view_through_lookback_window_days"),
    "phone_call_duration_seconds": ("phone_call_duration_seconds", "conversion_action.phone_call_duration_seconds"),
}


def plan_conversion_settings(ads: Ads, cid: str, login: str | None, conversion_action_id: str,
                             values: dict[str, Any], allow_goal_loss: bool, profile: Profile) -> Plan:
    values = {k: v for k, v in values.items() if v is not None}
    unknown = set(values) - set(SETTINGS)
    if unknown:
        raise WriteRefused(f"Unknown setting(s): {', '.join(sorted(unknown))}.")
    if not values:
        raise WriteRefused(f"Give at least one setting to change: {', '.join(SETTINGS)}.")
    if "counting_type" in values:
        values["counting_type"] = str(values["counting_type"]).upper()
        if values["counting_type"] not in COUNTING_TYPES:
            raise WriteRefused(f"counting_type must be {' or '.join(COUNTING_TYPES)}.")
    for key, (low, high) in {"click_through_lookback_window_days": (1, 90), "view_through_lookback_window_days": (1, 30),
                             "phone_call_duration_seconds": (0, 10000)}.items():
        if key in values:
            values[key] = int(values[key])
            if not low <= values[key] <= high:
                raise WriteRefused(f"{key} must be between {low} and {high}.")
    if "default_value" in values:
        values["default_value"] = round(float(values["default_value"]), 2)
        if values["default_value"] < 0:
            raise WriteRefused("default_value cannot be negative.")

    row = _load(ads, cid, login, [conversion_action_id], 1)[0]
    reason = mutable_reason(row, cid)
    if reason:
        raise WriteRefused(f"{_label(row)} cannot be changed: {reason}.")
    fixed = [k for k in values if k in IMMUTABLE_SETTINGS.get(row["conversion_action.type"], ())]
    if fixed:
        raise WriteRefused(f"{', '.join(fixed)} cannot be changed for {_label(row)}: for GA4 imports this comes from "
                           "Google Analytics. Change it in GA4.")
    if "phone_call_duration_seconds" in values and row["conversion_action.type"] not in CALL_TYPES:
        raise WriteRefused(f"phone_call_duration_seconds only works for call actions ({', '.join(CALL_TYPES)}). "
                           f"{_label(row)} is not one.")

    plan = Plan("ConversionActionService", "mutate_conversion_actions")
    client = ads.client(login)
    op = client.get_type("ConversionActionOperation")
    op.update.resource_name = row["conversion_action.resource_name"]
    paths = []
    for key, value in values.items():
        path, column = SETTINGS[key]
        old = row.get(column)
        if old == value:
            plan.warnings.append(f"{key} already is {value}, skipped.")
            continue
        if key == "counting_type":
            op.update.counting_type = client.enums.ConversionActionCountingTypeEnum[value]
        elif key in ("default_value", "always_use_default_value"):
            setattr(op.update.value_settings, path.split(".")[1], value)
        else:
            setattr(op.update, key, value)
        paths.append(path)
        plan.changes.append(Change(_label(row), key, old, value, row["conversion_action.resource_name"],
                                   object_type="conversion_action", object_id=str(row["conversion_action.id"]),
                                   name=row["conversion_action.name"]))
    if not paths:
        return plan
    client.copy_from(op.update_mask, _mask(*paths))
    plan.operations.append(op)

    changed = {c.field for c in plan.changes}
    if "primary_for_goal" in changed:
        if values["primary_for_goal"] is False:
            goal_check(ads, cid, login, [row], allow_goal_loss, plan)
        elif row["conversion_action.status"] == "ENABLED":
            plan.warnings.append(f"{_label(row)} becomes a primary action for goal {row['conversion_action.category']} "
                                 f"({row['conversion_action.origin']}). Smart bidding campaigns on that goal start "
                                 "learning again.")
        else:
            plan.warnings.append(f"{_label(row)} is {row['conversion_action.status']}: it only counts as primary "
                                 "once it is enabled.")
    if changed & {"default_value", "always_use_default_value", "counting_type"}:
        plan.warnings.append("Value and counting changes apply from now on. Past conversions are not recalculated.")
    return plan


def plan_revert(ads: Ads, cid: str, login: str | None, entry: dict[str, Any], profile: Profile) -> Plan:
    field, old, new, oid = entry.get("field"), entry.get("old"), entry.get("new"), entry.get("object_id")
    if entry.get("tool") == "set_conversion_action_status" or entry.get("original_tool") == "set_conversion_action_status":
        if new == "REMOVED":
            raise WriteRefused("Removing a conversion action is final. Google cannot enable it again, so this cannot be "
                               "reverted. Create a new conversion action in Google Ads if needed.")
        raise WriteRefused(f"The old status was {old}. The API cannot set {old}, so set it back in Google Ads.")
    plan = plan_conversion_settings(ads, cid, login, oid, {field: old}, False, profile)
    if not plan.changes:
        raise WriteRefused(f"Nothing to revert: {field} already is the old value '{old}'.")
    if str(plan.changes[0].old).lower() != str(new).lower():
        raise WriteRefused(f"Not reverted: the value changed since. Logged new value: {new!r}. Current value: "
                           f"{plan.changes[0].old!r}. Check in Google Ads and change it by hand if needed.")
    return plan
