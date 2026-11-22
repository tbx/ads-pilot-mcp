# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Bids: target CPA / target ROAS of a campaign, max CPC of keywords and ad groups.

- set_bidding_target changes only the target of a strategy the campaign already uses
  (Target CPA, Maximize conversions with target CPA, Target ROAS, Maximize conversion value with
  target ROAS). It never switches strategies and refuses portfolio (shared) strategies.
- set_keyword_bid sets max CPC only where bids are manual (Manual CPC, incl. enhanced CPC).
"""

from __future__ import annotations

import re

from .ads import Ads
from .config import format_customer_id
from .writes import MAX_ITEMS, Change, Plan, WriteRefused, _mask, _quote

CPA_FIELDS = {"TARGET_CPA": "target_cpa.target_cpa_micros",
              "MAXIMIZE_CONVERSIONS": "maximize_conversions.target_cpa_micros"}
ROAS_FIELDS = {"TARGET_ROAS": "target_roas.target_roas",
               "MAXIMIZE_CONVERSION_VALUE": "maximize_conversion_value.target_roas"}


def _money(micros: int | None) -> float:
    return round((micros or 0) / 1_000_000, 2)


def plan_bidding_target(ads: Ads, cid: str, login: str | None, campaign_id: str, target_cpa: float | None,
                        target_roas_percent: float | None, max_change_percent: float,
                        max_target_cpa: float | None) -> Plan:
    if (target_cpa is None) == (target_roas_percent is None):
        raise WriteRefused("Give exactly one of target_cpa (in account currency) or target_roas_percent (e.g. 400).")
    if not re.fullmatch(r"\d+", str(campaign_id).strip()):
        raise WriteRefused("campaign_id must be a number.")
    fields = ", ".join(f"campaign.{f}" for f in (*CPA_FIELDS.values(), *ROAS_FIELDS.values()))
    rows, _ = ads.search(cid, "SELECT campaign.resource_name, campaign.name, campaign.status, campaign.bidding_strategy, "
                         f"campaign.bidding_strategy_type, {fields}, customer.currency_code FROM campaign "
                         f"WHERE campaign.id = {campaign_id}", login_customer_id=login, max_rows=1)
    if not rows or rows[0]["campaign.status"] == "REMOVED":
        raise WriteRefused(f"Campaign {campaign_id} not found (or removed) in account {format_customer_id(cid)}.")
    row = rows[0]
    label = f"campaign '{row['campaign.name']}'"
    strategy = row["campaign.bidding_strategy_type"]
    currency = row.get("customer.currency_code") or ""
    if row.get("campaign.bidding_strategy"):
        raise WriteRefused(f"{label} uses a portfolio bid strategy shared by several campaigns. Change its targets with "
                           "manage_portfolio_bidding_strategy (action update); that shows every campaign it affects.")

    if target_cpa is not None:
        field = CPA_FIELDS.get(strategy)
        if not field:
            raise WriteRefused(f"{label} uses {strategy}, not a target CPA strategy. ads-pilot changes targets only, "
                               "it does not switch bid strategies.")
        old = _money(row[f"campaign.{field}"])
        if old <= 0:
            raise WriteRefused(f"{label} uses {strategy} without a target CPA. Setting one changes how the campaign "
                               "bids, which is a strategy change and not part of this tool.")
        new = round(float(target_cpa), 2)
        if new <= 0:
            raise WriteRefused("target_cpa must be greater than 0.")
        if max_target_cpa is not None and new > max_target_cpa:
            raise WriteRefused(f"{new:.2f} is above this profile's max_target_cpa of {max_target_cpa:.2f}.")
        old_text, new_text, value = f"{old:.2f} {currency}".strip(), f"{new:.2f} {currency}".strip(), int(round(new * 100)) * 10_000
        name = "target CPA"
    else:
        field = ROAS_FIELDS.get(strategy)
        if not field:
            raise WriteRefused(f"{label} uses {strategy}, not a target ROAS strategy. ads-pilot changes targets only, "
                               "it does not switch bid strategies.")
        old = round((row[f"campaign.{field}"] or 0) * 100, 1)
        if old <= 0:
            raise WriteRefused(f"{label} uses {strategy} without a target ROAS. Setting one changes how the campaign "
                               "bids, which is a strategy change and not part of this tool.")
        new = round(float(target_roas_percent), 1)
        if new <= 0:
            raise WriteRefused("target_roas_percent must be greater than 0.")
        old_text, new_text, value = f"{old:g}%", f"{new:g}%", new / 100
        name = "target ROAS"

    plan = Plan("CampaignService", "mutate_campaigns")
    if old == new:
        return plan
    change = (new - old) / old * 100
    if abs(change) > max_change_percent:
        raise WriteRefused(f"From {old_text} to {new_text} is a change of {change:+.0f}%. This profile allows at most "
                           f"{max_change_percent:.0f}% per step (max_target_change_percent). Change it in smaller steps.")
    client = ads.client(login)
    op = client.get_type("CampaignOperation")
    op.update.resource_name = row["campaign.resource_name"]
    group, attr = field.split(".")
    setattr(getattr(op.update, group), attr, value)
    client.copy_from(op.update_mask, _mask(field))
    plan.operations.append(op)
    plan.changes.append(Change(f"{name} of {label} ({strategy})", name.replace(" ", "_").lower(), old_text, new_text,
                               row["campaign.resource_name"], object_type="campaign_bidding_target",
                               object_id=str(campaign_id), name=row["campaign.name"]))
    plan.warnings.append("Smart bidding needs some days to adjust after a target change; judge results after 1-2 weeks.")
    return plan


def plan_keyword_bid(ads: Ads, cid: str, login: str | None, object_type: str, ids: list[str], max_cpc: float,
                     max_cpc_limit: float | None) -> Plan:
    if object_type not in ("keyword", "ad_group"):
        raise WriteRefused("object_type must be keyword or ad_group.")
    ids = [str(i).strip() for i in ids]
    if not ids or len(ids) > MAX_ITEMS:
        raise WriteRefused(f"Give between 1 and {MAX_ITEMS} IDs.")
    pattern, help_text = (r"\d+~\d+", "adGroupId~criterionId") if object_type == "keyword" else (r"\d+", "adGroupId")
    bad = [i for i in ids if not re.fullmatch(pattern, i)]
    if bad:
        raise WriteRefused(f"Invalid ID(s) {_quote(bad)}. Expected format: {help_text}.")
    new = round(float(max_cpc), 2)
    if new <= 0:
        raise WriteRefused("max_cpc must be greater than 0.")
    if max_cpc_limit is not None and new > max_cpc_limit:
        raise WriteRefused(f"{new:.2f} is above this profile's max_cpc of {max_cpc_limit:.2f}.")

    if object_type == "keyword":
        table, collection, extra = "ad_group_criterion", "adGroupCriteria", (
            "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.negative, "
            "ad_group_criterion.cpc_bid_micros, ad_group.cpc_bid_micros, ")
    else:
        table, collection, extra = "ad_group", "adGroups", "ad_group.cpc_bid_micros, "
    names = [f"customers/{cid}/{collection}/{i}" for i in dict.fromkeys(ids)]
    rows, _ = ads.search(cid, f"SELECT {table}.resource_name, {table}.status, {extra}ad_group.name, campaign.name, "
                         "campaign.status, campaign.bidding_strategy_type, customer.currency_code "
                         f"FROM {table} WHERE {table}.resource_name IN ({_quote(names)})",
                         login_customer_id=login, max_rows=MAX_ITEMS)
    found = {r[f"{table}.resource_name"]: r for r in rows}
    missing = [n.rsplit("/", 1)[-1] for n in names if n not in found]
    if missing:
        raise WriteRefused(f"Not found in account {format_customer_id(cid)}: {_quote(missing)}.")

    client = ads.client(login)
    service, method, operation = (("AdGroupCriterionService", "mutate_ad_group_criteria", "AdGroupCriterionOperation")
                                  if object_type == "keyword" else ("AdGroupService", "mutate_ad_groups", "AdGroupOperation"))
    plan = Plan(service, method, partial_failure=True)
    for rn in names:
        r = found[rn]
        currency = r.get("customer.currency_code") or ""
        if r["campaign.bidding_strategy_type"] != "MANUAL_CPC":
            raise WriteRefused(f"Campaign '{r['campaign.name']}' uses {r['campaign.bidding_strategy_type']}. Max CPC can "
                               "only be set with manual CPC bidding.")
        if r[f"{table}.status"] == "REMOVED" or r["campaign.status"] == "REMOVED":
            raise WriteRefused(f"{rn.rsplit('/', 1)[-1]} is removed and cannot be changed.")
        if object_type == "keyword":
            if r.get("ad_group_criterion.negative"):
                raise WriteRefused(f"{rn.rsplit('/', 1)[-1]} is a negative keyword, it has no bid.")
            label = (f"keyword '{r['ad_group_criterion.keyword.text']}' [{r['ad_group_criterion.keyword.match_type']}] "
                     f"in ad group '{r['ad_group.name']}'")
            own = r.get("ad_group_criterion.cpc_bid_micros")
            old = _money(own) if own else None
            old_text = f"{old:.2f} {currency}" if old else f"ad group default ({_money(r['ad_group.cpc_bid_micros']):.2f} {currency})"
            name = r["ad_group_criterion.keyword.text"]
        else:
            label = f"ad group '{r['ad_group.name']}' (campaign '{r['campaign.name']}')"
            old = _money(r["ad_group.cpc_bid_micros"])
            old_text = f"{old:.2f} {currency}"
            name = r["ad_group.name"]
        if old == new:
            plan.warnings.append(f"{label} already has max CPC {new:.2f}, skipped.")
            continue
        reference = old or _money(r.get("ad_group.cpc_bid_micros"))
        if max_cpc_limit is None and reference and new > 2 * reference:
            plan.warnings.append(f"{label}: {new:.2f} is more than double the current {reference:.2f}. "
                                 "Set max_cpc in the profile to cap bids.")
        op = client.get_type(operation)
        op.update.resource_name = rn
        op.update.cpc_bid_micros = int(round(new * 100)) * 10_000
        client.copy_from(op.update_mask, _mask("cpc_bid_micros"))
        plan.operations.append(op)
        plan.changes.append(Change(label, "max_cpc", old_text.strip(), f"{new:.2f} {currency}".strip(), rn,
                                   object_type=f"{object_type}_bid", object_id=rn.rsplit("/", 1)[-1], name=name))
    return plan
