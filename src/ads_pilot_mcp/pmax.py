# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Performance Max: text assets of an asset group, search themes, brand exclusions.

- Text assets: a given list REPLACES the advertiser-created assets of that field (like
  update_responsive_search_ad). New texts become new text assets (or reuse identical existing ones),
  texts that are no longer wanted are unlinked. Assets themselves are never deleted, and
  automatically created assets are left alone. Everything goes in one GoogleAdsService.mutate request.
- Business name: with brand guidelines on, it belongs to the campaign (CampaignAsset), otherwise
  to the asset group.
- Search themes: AssetGroupSignal with search_theme.
- Brand exclusions: a SharedSet of type BRANDS attached to the campaign via CampaignSharedSet.
"""

from __future__ import annotations

import re
from typing import Any

from .ads import Ads
from .config import format_customer_id
from .writes import Change, Plan, WriteRefused

# field -> (min count, max count, max characters)
TEXT_LIMITS = {
    "HEADLINE": (3, 15, 30),
    "LONG_HEADLINE": (1, 5, 90),
    "DESCRIPTION": (2, 5, 90),
    "BUSINESS_NAME": (1, 1, 25),
}
MAX_SEARCH_THEMES = 50


def _asset_group(ads: Ads, cid: str, login: str | None, asset_group_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d+", str(asset_group_id).strip()):
        raise WriteRefused("asset_group_id must be a number.")
    rows, _ = ads.search(cid, "SELECT asset_group.resource_name, asset_group.name, asset_group.status, campaign.id, "
                         "campaign.resource_name, campaign.name, campaign.status, campaign.advertising_channel_type, "
                         f"campaign.brand_guidelines_enabled FROM asset_group WHERE asset_group.id = {asset_group_id}",
                         login_customer_id=login, max_rows=1)
    if not rows or "REMOVED" in (rows[0]["asset_group.status"], rows[0]["campaign.status"]):
        raise WriteRefused(f"Asset group {asset_group_id} not found, or it or its campaign is removed, in account "
                           f"{format_customer_id(cid)}.")
    if rows[0]["campaign.advertising_channel_type"] != "PERFORMANCE_MAX":
        raise WriteRefused(f"Asset group {asset_group_id} is not in a Performance Max campaign.")
    return rows[0]


def _group_label(row: dict) -> str:
    return f"asset group '{row['asset_group.name']}' (campaign '{row['campaign.name']}')"


def _check_texts(field: str, texts: list[str]) -> list[str]:
    low, high, chars = TEXT_LIMITS[field]
    texts = [" ".join(str(t).split()) for t in texts]
    name = field.lower()
    if not low <= len(texts) <= high:
        raise WriteRefused(f"{name}: give between {low} and {high} texts (got {len(texts)}). The list replaces all "
                           f"current advertiser {name}s.")
    for t in texts:
        if not t:
            raise WriteRefused(f"{name}: empty text.")
        if len(t) > chars:
            raise WriteRefused(f"{name}: '{t}' has {len(t)} characters, the limit is {chars}.")
    if len({t.lower() for t in texts}) != len(texts):
        raise WriteRefused(f"{name}: duplicate texts.")
    return texts


# ---- text assets -------------------------------------------------------------


def plan_asset_group_text_assets(ads: Ads, cid: str, login: str | None, asset_group_id: str,
                                 headlines: list[str] | None, long_headlines: list[str] | None,
                                 descriptions: list[str] | None, business_name: str | None) -> Plan:
    wanted = {f: _check_texts(f, v) for f, v in (("HEADLINE", headlines), ("LONG_HEADLINE", long_headlines),
                                                 ("DESCRIPTION", descriptions)) if v is not None}
    if business_name is not None:
        wanted["BUSINESS_NAME"] = _check_texts("BUSINESS_NAME", [business_name])
    if not wanted:
        raise WriteRefused("Give at least one of headlines, long_headlines, descriptions, business_name.")

    group = _asset_group(ads, cid, login, asset_group_id)
    label = _group_label(group)
    on_campaign = bool(group.get("campaign.brand_guidelines_enabled"))

    current: dict[str, list[dict]] = {f: [] for f in wanted}
    rows, _ = ads.search(cid, "SELECT asset_group_asset.resource_name, asset_group_asset.field_type, asset.resource_name, "
                         "asset.text_asset.text FROM asset_group_asset WHERE "
                         f"asset_group.id = {asset_group_id} AND asset_group_asset.status != 'REMOVED' AND "
                         "asset_group_asset.source = ADVERTISER AND asset_group_asset.field_type IN "
                         "(HEADLINE, LONG_HEADLINE, DESCRIPTION, BUSINESS_NAME)", login_customer_id=login, max_rows=500)
    for r in rows:
        f = r["asset_group_asset.field_type"]
        if f in current and not (f == "BUSINESS_NAME" and on_campaign):
            current[f].append({"link": r["asset_group_asset.resource_name"], "asset": r["asset.resource_name"],
                               "text": r["asset.text_asset.text"] or ""})
    if "BUSINESS_NAME" in wanted and on_campaign:
        rows, _ = ads.search(cid, "SELECT campaign.id, campaign_asset.resource_name, asset.resource_name, "
                             f"asset.text_asset.text FROM campaign_asset WHERE campaign.id = {group['campaign.id']} AND "
                             "campaign_asset.field_type = BUSINESS_NAME AND campaign_asset.status != 'REMOVED'",
                             login_customer_id=login, max_rows=5)
        current["BUSINESS_NAME"] = [{"link": r["campaign_asset.resource_name"], "asset": r["asset.resource_name"],
                                     "text": r["asset.text_asset.text"] or ""} for r in rows]

    # Reuse identical text assets that already exist in the account, create the rest.
    all_new = sorted({t for f, texts in wanted.items() for t in texts
                      if t.lower() not in {c["text"].lower() for c in current[f]}})
    reuse: dict[str, str] = {}
    if all_new:
        quoted = ", ".join("'" + t.replace("\\", "\\\\").replace("'", "\\'") + "'" for t in all_new)
        found, _ = ads.search(cid, "SELECT asset.resource_name, asset.text_asset.text FROM asset WHERE asset.type = TEXT "
                              f"AND asset.text_asset.text IN ({quoted})", login_customer_id=login, max_rows=500)
        reuse = {r["asset.text_asset.text"]: r["asset.resource_name"] for r in found}

    client = ads.client(login)
    plan = Plan("GoogleAdsService", "mutate")
    creates, links, unlinks = [], [], []
    temp_id = -1
    for field, texts in wanted.items():
        have = {c["text"].lower(): c for c in current[field]}
        old_view = [c["text"] for c in current[field]]
        if [t.lower() for t in texts] == [t.lower() for t in old_view]:
            continue
        for text in texts:
            if text.lower() in have:
                continue
            asset_rn = reuse.get(text)
            if not asset_rn:
                asset_rn = f"customers/{cid}/assets/{temp_id}"
                temp_id -= 1
                op = client.get_type("MutateOperation")
                op.asset_operation.create.resource_name = asset_rn
                op.asset_operation.create.text_asset.text = text
                creates.append(op)
            op = client.get_type("MutateOperation")
            field_enum = client.enums.AssetFieldTypeEnum[field]
            if field == "BUSINESS_NAME" and on_campaign:
                op.campaign_asset_operation.create.campaign = group["campaign.resource_name"]
                op.campaign_asset_operation.create.asset = asset_rn
                op.campaign_asset_operation.create.field_type = field_enum
            else:
                op.asset_group_asset_operation.create.asset_group = group["asset_group.resource_name"]
                op.asset_group_asset_operation.create.asset = asset_rn
                op.asset_group_asset_operation.create.field_type = field_enum
            links.append(op)
        keep = {t.lower() for t in texts}
        for c in current[field]:
            if c["text"].lower() in keep:
                continue
            op = client.get_type("MutateOperation")
            if field == "BUSINESS_NAME" and on_campaign:
                op.campaign_asset_operation.remove = c["link"]
            else:
                op.asset_group_asset_operation.remove = c["link"]
            unlinks.append(op)
        where = f"campaign '{group['campaign.name']}' (brand guidelines)" if field == "BUSINESS_NAME" and on_campaign \
            else label
        plan.changes.append(Change(f"{field.lower()}s of {where}", field.lower(), old_view, texts,
                                   group["asset_group.resource_name"], object_type="asset_group_text_assets",
                                   object_id=str(asset_group_id), name=group["asset_group.name"]))
    # New assets first, then new links, then unlinks: the counts stay valid inside the one request.
    plan.operations = creates + links + unlinks
    if plan.changes:
        plan.warnings.append("Old texts are only unlinked, the assets stay in the account. Automatically created "
                             "assets are not touched. Google reviews new texts before they show.")
    return plan


# ---- search themes -----------------------------------------------------------


def plan_search_themes(ads: Ads, cid: str, login: str | None, asset_group_id: str, add: list[str],
                       remove: list[str]) -> Plan:
    add = [" ".join(str(t).split()) for t in (add or []) if str(t).strip()]
    remove = [" ".join(str(t).split()) for t in (remove or []) if str(t).strip()]
    if not (add or remove):
        raise WriteRefused("Give search themes to add or remove.")
    if len(add) + len(remove) > MAX_SEARCH_THEMES:
        raise WriteRefused(f"At most {MAX_SEARCH_THEMES} search themes per call.")
    group = _asset_group(ads, cid, login, asset_group_id)
    label = _group_label(group)
    rows, _ = ads.search(cid, "SELECT asset_group_signal.resource_name, asset_group_signal.search_theme.text FROM "
                         f"asset_group_signal WHERE asset_group.id = {asset_group_id}", login_customer_id=login,
                         max_rows=1000)
    existing = {r["asset_group_signal.search_theme.text"].lower(): r["asset_group_signal.resource_name"]
                for r in rows if r.get("asset_group_signal.search_theme.text")}

    client = ads.client(login)
    plan = Plan("AssetGroupSignalService", "mutate_asset_group_signals", partial_failure=True)
    after = len(existing)
    for text in dict.fromkeys(add):
        if text.lower() in existing:
            plan.warnings.append(f"Search theme '{text}' already in {label}, skipped.")
            continue
        op = client.get_type("AssetGroupSignalOperation")
        op.create.asset_group = group["asset_group.resource_name"]
        op.create.search_theme.text = text
        plan.operations.append(op)
        plan.changes.append(Change(f"search theme '{text}' in {label}", "search_theme", None, "added",
                                   group["asset_group.resource_name"], object_type="asset_group_search_theme",
                                   object_id=str(asset_group_id), name=text))
        after += 1
    for text in dict.fromkeys(remove):
        rn = existing.get(text.lower())
        if not rn:
            plan.warnings.append(f"Search theme '{text}' is not in {label}, nothing to remove.")
            continue
        op = client.get_type("AssetGroupSignalOperation")
        op.remove = rn
        plan.operations.append(op)
        plan.changes.append(Change(f"search theme '{text}' in {label}", "search_theme", "present", "removed", rn,
                                   object_type="asset_group_search_theme", object_id=str(asset_group_id), name=text))
        after -= 1
    if after > MAX_SEARCH_THEMES:
        plan.warnings.append(f"{label} would have {after} search themes; Google allows {MAX_SEARCH_THEMES} per "
                             "asset group, so some additions will fail.")
    return plan


# ---- brand exclusions --------------------------------------------------------


def plan_brand_exclusions(ads: Ads, cid: str, login: str | None, campaign_id: str, shared_set_id: str,
                          action: str) -> Plan:
    if action not in ("attach", "detach"):
        raise WriteRefused("action must be attach or detach.")
    for value, name in ((campaign_id, "campaign_id"), (shared_set_id, "shared_set_id")):
        if not re.fullmatch(r"\d+", str(value or "").strip()):
            raise WriteRefused(f"{name} must be a number.")
    lists, _ = ads.search(cid, "SELECT shared_set.resource_name, shared_set.name, shared_set.type, shared_set.status, "
                          f"shared_set.member_count FROM shared_set WHERE shared_set.id = {shared_set_id}",
                          login_customer_id=login, max_rows=1)
    if not lists or lists[0]["shared_set.status"] != "ENABLED" or lists[0]["shared_set.type"] != "BRANDS":
        raise WriteRefused(f"Brand list {shared_set_id} not found (or removed, or not a brand list) in account "
                           f"{format_customer_id(cid)}. Brand lists are created in Google Ads under "
                           "Tools > Shared library > Brand lists.")
    camps, _ = ads.search(cid, "SELECT campaign.resource_name, campaign.name, campaign.status, "
                          f"campaign.advertising_channel_type FROM campaign WHERE campaign.id = {campaign_id}",
                          login_customer_id=login, max_rows=1)
    if not camps or camps[0]["campaign.status"] == "REMOVED":
        raise WriteRefused(f"Campaign {campaign_id} not found (or removed) in account {format_customer_id(cid)}.")
    lst, camp = lists[0], camps[0]
    if camp["campaign.advertising_channel_type"] != "PERFORMANCE_MAX":
        raise WriteRefused(f"Campaign '{camp['campaign.name']}' is a {camp['campaign.advertising_channel_type']} "
                           "campaign. This tool handles brand exclusions of Performance Max campaigns.")
    linked, _ = ads.search(cid, "SELECT campaign_shared_set.resource_name FROM campaign_shared_set WHERE "
                           f"campaign.id = {campaign_id} AND shared_set.id = {shared_set_id} AND "
                           "campaign_shared_set.status = 'ENABLED'", login_customer_id=login, max_rows=1)
    label = f"brand list '{lst['shared_set.name']}' ({shared_set_id}) on campaign '{camp['campaign.name']}' ({campaign_id})"
    client = ads.client(login)
    plan = Plan("CampaignSharedSetService", "mutate_campaign_shared_sets")
    op = client.get_type("CampaignSharedSetOperation")
    if action == "attach":
        if linked:
            plan.warnings.append(f"The {label} is already attached.")
            return plan
        op.create.campaign = camp["campaign.resource_name"]
        op.create.shared_set = lst["shared_set.resource_name"]
        old, new = "not attached", "attached"
        plan.warnings.append(f"Ads of this campaign no longer show for searches about the "
                             f"{lst['shared_set.member_count']} brands in the list.")
    else:
        if not linked:
            plan.warnings.append(f"The {label} is not attached, nothing to detach.")
            return plan
        op.remove = linked[0]["campaign_shared_set.resource_name"]
        old, new = "attached", "detached"
    plan.operations.append(op)
    plan.changes.append(Change(label, "brand_exclusion_list", old, new, camp["campaign.resource_name"],
                               object_type="campaign_brand_exclusion", object_id=str(campaign_id),
                               name=f"{lst['shared_set.name']} ({shared_set_id})"))
    return plan
