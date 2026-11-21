# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""revert_change: put the old value of one logged change back.

The inverse change is built with the same plan functions as the original tools, so it gets the
same checks. Before reverting, the current value must still equal the logged new value; if someone
changed it in the meantime, nothing is reverted and both values are shown.
Changes that cannot be undone cleanly (they would need a delete) are refused with a clear reason.
"""

from __future__ import annotations

import re
from typing import Any

from . import bidding, conversions, negatives, pmax, strategy, targeting, writes
from .ads import Ads
from .config import Profile
from .writes import Plan, WriteRefused

NEG_LEVELS = {"campaign_negative_keyword": "campaign", "ad_group_negative_keyword": "ad_group",
              "shared_list_negative_keyword": "shared_list", "account_negative_keyword": "account"}
NOT_REVERTIBLE = {
    "add_keywords": "Keywords cannot be deleted with ads-pilot. Pause the keyword instead (set_status).",
}


def _number(text: Any) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", str(text or ""))
    if not match:
        raise WriteRefused(f"Cannot read a number from the logged value '{text}'.")
    return float(match.group())


def _percent(text: str) -> float:
    return 0.0 if str(text).startswith("no adjustment") else _number(text)


def _last_id(resource: str) -> str:
    return resource.rsplit("/", 1)[-1]


def _strip_pin(text: str) -> str:
    return re.sub(r" \[pinned [A-Z_0-9]+\]$", "", text)


def _expect_current(plan: Plan, entry: dict) -> Plan:
    """The value we would change away from must be the logged new value."""
    if not plan.changes:
        raise WriteRefused(f"Nothing to revert: the current value already is the old value '{entry['old']}', or the "
                           "object no longer exists in that form.")
    current = plan.changes[0].old
    if _norm(current) != _norm(entry["new"]):
        raise WriteRefused(f"Not reverted: the value changed since. Logged new value: {entry['new']!r}. Current value: "
                           f"{current!r}. Check in Google Ads and change it by hand if needed.")
    return plan


def _norm(value: Any) -> Any:
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value]
    return str(value).strip().lower() if value is not None else None


def plan_revert(ads: Ads, cid: str, login: str | None, entry: dict, profile: Profile) -> Plan:
    tool = entry.get("original_tool") if entry.get("tool") == "revert_change" else entry.get("tool")
    otype, oid, field, old, new = (entry.get("object_type") or "", entry.get("object_id") or "",
                                   entry.get("field"), entry.get("old"), entry.get("new"))
    resource, obj = entry.get("resource") or "", entry.get("object") or ""
    if tool in NOT_REVERTIBLE:
        raise WriteRefused(f"This change cannot be reverted cleanly. {NOT_REVERTIBLE[tool]}")

    if tool == "set_status":
        ids = [oid or _last_id(resource)]
        return _expect_current(writes.plan_set_status(ads, cid, login, otype or "keyword", ids, old,
                                                      profile.allow_campaign_status_change), entry)

    if tool == "set_campaign_budget":
        rows, _ = ads.search(cid, "SELECT campaign_budget.amount_micros, customer.currency_code FROM campaign_budget "
                             f"WHERE campaign_budget.resource_name = '{resource}'", login_customer_id=login, max_rows=1)
        if not rows:
            raise WriteRefused("The budget no longer exists.")
        currency = rows[0].get("customer.currency_code") or ""
        current = f"{(rows[0]['campaign_budget.amount_micros'] or 0) / 1_000_000:.2f} {currency}".strip()
        plan = Plan("CampaignBudgetService", "mutate_campaign_budgets")
        client = ads.client(login)
        op = client.get_type("CampaignBudgetOperation")
        op.update.resource_name = resource
        op.update.amount_micros = int(round(_number(old) * 100)) * 10_000
        client.copy_from(op.update_mask, writes._mask("amount_micros"))
        plan.operations.append(op)
        plan.changes.append(writes.Change(obj, field, current, old, resource, object_type="campaign_budget",
                                          object_id=oid, name=entry.get("name") or ""))
        plan.warnings.append("Revert restores the old budget without the step limit, but it stays a dry run first.")
        return _expect_current(plan, entry)

    if tool in ("add_negative_keywords", "remove_negative_keywords"):
        level = NEG_LEVELS.get(otype)
        match = re.match(r"(.*) \[(EXACT|PHRASE|BROAD)\]$", entry.get("name") or "")
        if not level or not match:
            raise WriteRefused("This negative keyword entry lacks the details needed to revert it.")
        ids = {"campaign_id": oid if level == "campaign" else None, "ad_group_id": oid if level == "ad_group" else None,
               "shared_set_id": oid if level == "shared_list" else None}
        kw = [{"text": match.group(1), "match_type": match.group(2)}]
        build = negatives.plan_remove_negative_keywords if tool == "add_negative_keywords" else negatives.plan_add_negative_keywords
        plan = build(ads, cid, login, level, ids["campaign_id"], ids["ad_group_id"], ids["shared_set_id"], kw, 1)
        if not plan.changes:
            raise WriteRefused("Nothing to revert: the negative keyword is already in its old state.")
        return plan

    if tool == "manage_negative_keyword_list" or tool == "set_brand_exclusions":
        if old is None:
            raise WriteRefused("Creating a list cannot be reverted: lists are never deleted. Detach it instead.")
        list_id = re.search(r"\((\d+)\)$", entry.get("name") or "")
        if not list_id:
            raise WriteRefused("This entry lacks the list ID needed to revert it.")
        action = "detach" if new == "attached" else "attach"
        if tool == "set_brand_exclusions":
            plan = pmax.plan_brand_exclusions(ads, cid, login, oid, list_id.group(1), action)
        else:
            plan = negatives.plan_manage_negative_keyword_list(ads, cid, login, action, None, list_id.group(1), oid)
        return _expect_current(plan, entry)

    if tool == "set_device_bid_adjustment":
        device = obj.split(" ", 1)[0]
        campaign_id = re.search(r"/(?:campaignCriteria|campaigns)/(\d+)", resource).group(1)
        return _expect_current(writes.plan_device_bid(ads, cid, login, campaign_id, device, _percent(old)), entry)

    if tool == "set_ad_schedule_bid_adjustment":
        if old == "no time window yet":
            raise WriteRefused("A newly added time window cannot be reverted: removing it would delete it. Remove it in "
                               "Google Ads or set its adjustment to 0%.")
        m = re.match(r"ad schedule (\w+) (\d\d:\d\d)-(\d\d:\d\d) in", obj)
        campaign_id = re.search(r"/(?:campaignCriteria|campaigns)/(\d+)", resource).group(1)
        return _expect_current(writes.plan_schedule_bid(ads, cid, login, campaign_id, [m.group(1)], m.group(2),
                                                        m.group(3), _percent(old)), entry)

    if tool == "update_responsive_search_ad":
        args = {"final_url": None, "headlines": None, "descriptions": None}
        if field == "final_url":
            args["final_url"] = (old or [None])[0]
        else:
            args[field] = [_strip_pin(t) for t in old]
        plan = _expect_current(writes.plan_update_rsa(ads, cid, login, _last_id(resource), **args), entry)
        if any("[pinned" in t for t in (old or []) if isinstance(t, str)):
            plan.warnings.append("Pins come back only for texts that are unchanged; check pinning in Google Ads.")
        return plan

    if tool == "set_location_targeting":
        if field == "positive_geo_target_type":
            reverse = {label: key for key, label in targeting.GEO_LABELS.items()}
            return _expect_current(targeting.plan_location_targeting(
                ads, cid, login, oid, [], [], [], None, reverse.get(old, old), profile.allow_location_changes), entry)
        geo = re.search(r"\([^,]+, (\d+)\) in campaign", obj)
        if not geo:
            raise WriteRefused("This location entry lacks the geo target ID needed to revert it.")
        gid = geo.group(1)
        if new in ("targeted", "excluded"):
            add, exclude, remove = [], [], [gid]
        elif old == "targeted":
            add, exclude, remove = [gid], [], []
        else:
            add, exclude, remove = [], [gid], []
        plan = targeting.plan_location_targeting(ads, cid, login, oid, add, exclude, remove, None, None,
                                                 profile.allow_location_changes)
        if not plan.changes:
            raise WriteRefused("Nothing to revert: the location is already in its old state.")
        return plan

    if tool == "set_language_targeting":
        lang = re.search(r"\(\w+, (\d+)\) in campaign", obj)
        if not lang:
            raise WriteRefused("This language entry lacks the language ID needed to revert it.")
        add, remove = ([], [lang.group(1)]) if new == "targeted" else ([lang.group(1)], [])
        plan = targeting.plan_language_targeting(ads, cid, login, oid, add, remove)
        if not plan.changes:
            raise WriteRefused("Nothing to revert: the language is already in its old state.")
        return plan

    if tool == "set_network_settings":
        flag = old == "on"
        args = {"search_partners": flag if field == "target_search_network" else None,
                "display_network": flag if field == "target_content_network" else None}
        return _expect_current(targeting.plan_network_settings(ads, cid, login, oid, **args), entry)

    if tool == "update_asset_group_text_assets":
        args = {"headlines": None, "long_headlines": None, "descriptions": None, "business_name": None}
        key = {"headline": "headlines", "long_headline": "long_headlines", "description": "descriptions",
               "business_name": "business_name"}[field]
        args[key] = old[0] if key == "business_name" else old
        return _expect_current(pmax.plan_asset_group_text_assets(ads, cid, login, oid, **args), entry)

    if tool == "set_search_themes":
        add, remove = ([], [entry.get("name")]) if new == "added" else ([entry.get("name")], [])
        plan = pmax.plan_search_themes(ads, cid, login, oid, add, remove)
        if not plan.changes:
            raise WriteRefused("Nothing to revert: the search theme is already in its old state.")
        return plan

    if tool == "set_bidding_target":
        cpa = _number(old) if field == "target_cpa" else None
        roas = _number(old) if field == "target_roas" else None
        plan = bidding.plan_bidding_target(ads, cid, login, oid, cpa, roas, float("inf"), None)
        return _expect_current(plan, entry)

    if tool == "set_bidding_strategy":
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise WriteRefused("This entry lacks the strategy details needed to revert it.")
        args = strategy.revert_arguments(old)
        plan = strategy.plan_bidding_strategy(ads, cid, login, oid, **args, profile=profile, logged=[], is_revert=True)
        if not plan.changes:
            raise WriteRefused("Nothing to revert: the campaign already uses the old strategy with the old values.")
        current = plan.changes[0].old
        if current != new:
            raise WriteRefused(f"Not reverted: the strategy changed since. Logged new state: {new}. Current state: "
                               f"{current}. Check in Google Ads and change it by hand if needed.")
        if old.get("strategy") in ("TARGET_CPA", "TARGET_ROAS"):
            plan.warnings.append(f"The old strategy {old['strategy']} comes back as {args['strategy']} with the same "
                                 "target, which is how Google offers it today.")
        return plan

    if tool in conversions.TOOLS:
        return conversions.plan_revert(ads, cid, login, entry, profile)

    if tool == "set_keyword_bid":
        if str(old).startswith("ad group default"):
            raise WriteRefused("The keyword used the ad group default bid before. Clearing a keyword bid is not "
                               "supported; set a max CPC instead.")
        object_type = "keyword" if otype == "keyword_bid" else "ad_group"
        return _expect_current(bidding.plan_keyword_bid(ads, cid, login, object_type, [oid], _number(old), None), entry)

    raise WriteRefused(f"Changes of '{tool}' cannot be reverted automatically.")
