# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Negative keywords: add, remove, and manage negative keyword lists.

Levels:
- campaign:    CampaignCriterion with negative=true. Works for Search, Shopping and
               Performance Max (campaign-level negatives for PMax exist since API v20).
- ad_group:    AdGroupCriterion with negative=true (not for Performance Max, it has no ad groups).
- shared_list: SharedCriterion in a negative keyword list (SharedSet of type NEGATIVE_KEYWORDS).
- account:     SharedCriterion in the account-level list (SharedSet of type
               ACCOUNT_LEVEL_NEGATIVE_KEYWORDS, linked via CustomerNegativeCriterion).

Same rules as every write: dry run first, confirm token, allowlist, change log.
Removing is allowed here, because it undoes mistakes.
"""

from __future__ import annotations

import re
from typing import Any

from .ads import Ads
from .config import format_customer_id
from .writes import Change, Plan, WriteRefused

LEVELS = ("campaign", "ad_group", "shared_list", "account")
MATCH_TYPES = ("EXACT", "PHRASE", "BROAD")
MAX_CONFLICTS_SHOWN = 10


# ---- targets -----------------------------------------------------------------


def _need_id(value: str | None, name: str, level: str) -> str:
    if value is None or not re.fullmatch(r"\d+", str(value).strip()):
        raise WriteRefused(f"level '{level}' needs {name} (a number).")
    return str(value).strip()


def resolve_target(ads: Ads, cid: str, login: str | None, level: str, campaign_id: str | None,
                   ad_group_id: str | None, shared_set_id: str | None) -> dict[str, Any]:
    """Find the object the negatives belong to. Returns label, resource, IDs and the campaigns it affects."""
    if level not in LEVELS:
        raise WriteRefused(f"level must be one of: {', '.join(LEVELS)}.")

    if level == "campaign":
        campaign_id = _need_id(campaign_id, "campaign_id", level)
        rows, _ = ads.search(cid, "SELECT campaign.resource_name, campaign.name, campaign.status, "
                             f"campaign.advertising_channel_type FROM campaign WHERE campaign.id = {campaign_id}",
                             login_customer_id=login, max_rows=1)
        if not rows or rows[0]["campaign.status"] == "REMOVED":
            raise WriteRefused(f"Campaign {campaign_id} not found (or removed) in account {format_customer_id(cid)}.")
        r = rows[0]
        return {"level": level, "id": campaign_id, "resource": r["campaign.resource_name"],
                "label": f"campaign '{r['campaign.name']}' ({campaign_id}, {r['campaign.advertising_channel_type']})",
                "object_type": "campaign_negative_keyword", "campaign_filter": f"campaign.id = {campaign_id}"}

    if level == "ad_group":
        ad_group_id = _need_id(ad_group_id, "ad_group_id", level)
        rows, _ = ads.search(cid, "SELECT ad_group.resource_name, ad_group.name, ad_group.status, campaign.name, "
                             f"campaign.status FROM ad_group WHERE ad_group.id = {ad_group_id}",
                             login_customer_id=login, max_rows=1)
        if not rows or "REMOVED" in (rows[0]["ad_group.status"], rows[0]["campaign.status"]):
            raise WriteRefused(f"Ad group {ad_group_id} not found, or it or its campaign is removed, "
                               f"in account {format_customer_id(cid)}.")
        r = rows[0]
        return {"level": level, "id": ad_group_id, "resource": r["ad_group.resource_name"],
                "label": f"ad group '{r['ad_group.name']}' ({ad_group_id}) in campaign '{r['campaign.name']}'",
                "object_type": "ad_group_negative_keyword", "campaign_filter": f"ad_group.id = {ad_group_id}"}

    if level == "shared_list":
        shared_set_id = _need_id(shared_set_id, "shared_set_id", level)
        rows, _ = ads.search(cid, "SELECT shared_set.resource_name, shared_set.name, shared_set.type, shared_set.status "
                             f"FROM shared_set WHERE shared_set.id = {shared_set_id}", login_customer_id=login, max_rows=1)
        if not rows or rows[0]["shared_set.status"] != "ENABLED":
            raise WriteRefused(f"Negative keyword list {shared_set_id} not found (or removed) in account "
                               f"{format_customer_id(cid)}.")
        if rows[0]["shared_set.type"] != "NEGATIVE_KEYWORDS":
            raise WriteRefused(f"Shared set {shared_set_id} is a {rows[0]['shared_set.type']} list, "
                               "not a negative keyword list.")
        r = rows[0]
        linked, _ = ads.search(cid, "SELECT campaign.id FROM campaign_shared_set WHERE "
                               f"shared_set.id = {shared_set_id} AND campaign_shared_set.status = 'ENABLED' "
                               "AND campaign.status != 'REMOVED'", login_customer_id=login, max_rows=1000)
        ids = [str(x["campaign.id"]) for x in linked]
        return {"level": level, "id": shared_set_id, "resource": r["shared_set.resource_name"],
                "label": f"negative keyword list '{r['shared_set.name']}' ({shared_set_id}, used by {len(ids)} campaigns)",
                "object_type": "shared_list_negative_keyword",
                "campaign_filter": f"campaign.id IN ({', '.join(ids)})" if ids else None}

    # account
    rows, _ = ads.search(cid, "SELECT shared_set.resource_name, shared_set.id, shared_set.name FROM shared_set "
                         "WHERE shared_set.type = ACCOUNT_LEVEL_NEGATIVE_KEYWORDS AND shared_set.status = 'ENABLED'",
                         login_customer_id=login, max_rows=1)
    if not rows:
        raise WriteRefused(
            f"Account {format_customer_id(cid)} has no account-level negative keyword list yet. Create it once in "
            "Google Ads (Admin > Account settings > Negative keywords), then this works."
        )
    r = rows[0]
    return {"level": level, "id": str(r["shared_set.id"]), "resource": r["shared_set.resource_name"],
            "label": f"account-level negative keywords of {format_customer_id(cid)}",
            "object_type": "account_negative_keyword", "campaign_filter": ""}


def existing_negatives(ads: Ads, cid: str, login: str | None, target: dict) -> dict[tuple[str, str], str]:
    """(text lower, match type) -> resource name of the negative keywords already at the target."""
    level, tid = target["level"], target["id"]
    if level == "campaign":
        table, where = "campaign_criterion", (f"campaign.id = {tid} AND campaign_criterion.type = KEYWORD AND "
                                              "campaign_criterion.negative = TRUE AND campaign_criterion.status != 'REMOVED'")
    elif level == "ad_group":
        table, where = "ad_group_criterion", (f"ad_group.id = {tid} AND ad_group_criterion.type = KEYWORD AND "
                                              "ad_group_criterion.negative = TRUE AND ad_group_criterion.status != 'REMOVED'")
    else:
        table, where = "shared_criterion", f"shared_set.id = {tid} AND shared_criterion.type = KEYWORD"
    rows, _ = ads.search(cid, f"SELECT {table}.resource_name, {table}.keyword.text, {table}.keyword.match_type "
                         f"FROM {table} WHERE {where}", login_customer_id=login, max_rows=20000)
    return {(r[f"{table}.keyword.text"].lower(), r[f"{table}.keyword.match_type"]): r[f"{table}.resource_name"]
            for r in rows}


# ---- matching ----------------------------------------------------------------


def _words(text: str) -> list[str]:
    return [w.strip('+"[]').lower() for w in text.split() if w.strip('+"[]')]


def would_block(negative: str, match_type: str, positive: str) -> bool:
    """Rough check: would this negative keyword stop searches that this positive keyword is meant for?"""
    neg, pos = _words(negative), _words(positive)
    if not neg:
        return False
    if match_type == "EXACT":
        return neg == pos
    if match_type == "PHRASE":
        return any(pos[i:i + len(neg)] == neg for i in range(len(pos) - len(neg) + 1))
    return set(neg) <= set(pos)


def conflicts(ads: Ads, cid: str, login: str | None, target: dict, new: list[tuple[str, str]]) -> list[str]:
    """Warnings for active own keywords the new negatives would block."""
    if target["campaign_filter"] is None:  # a list that no campaign uses
        return []
    where = ("ad_group_criterion.type = KEYWORD AND ad_group_criterion.negative = FALSE AND "
             "ad_group_criterion.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'")
    if target["campaign_filter"]:
        where += f" AND {target['campaign_filter']}"
    rows, _ = ads.search(cid, "SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
                         f"ad_group.name, campaign.name FROM ad_group_criterion WHERE {where}",
                         login_customer_id=login, max_rows=10000)
    hits = []
    for text, match in new:
        for r in rows:
            if would_block(text, match, r["ad_group_criterion.keyword.text"]):
                hits.append(f"Negative '{text}' [{match}] would block your active keyword "
                            f"'{r['ad_group_criterion.keyword.text']}' [{r['ad_group_criterion.keyword.match_type']}] "
                            f"in ad group '{r['ad_group.name']}' (campaign '{r['campaign.name']}').")
    if len(hits) > MAX_CONFLICTS_SHOWN:
        hits = hits[:MAX_CONFLICTS_SHOWN] + [f"... and {len(hits) - MAX_CONFLICTS_SHOWN} more conflicts."]
    return hits


def _clean_keywords(keywords: list[dict], limit: int) -> list[tuple[str, str]]:
    if not keywords:
        raise WriteRefused("Give at least one keyword.")
    if len(keywords) > limit:
        raise WriteRefused(f"{len(keywords)} keywords, but this profile allows at most {limit} per call "
                           "(max_negative_keywords_per_call). Split them into several calls.")
    out, seen = [], set()
    for kw in keywords:
        text = " ".join(str(kw.get("text", "")).split())
        match = str(kw.get("match_type", "")).upper()
        if not text:
            raise WriteRefused("Empty keyword text.")
        if re.search(r'^[\["]|[\]"]$', text):
            raise WriteRefused(f"'{text}': give the plain text without brackets or quotes and set match_type instead.")
        if match not in MATCH_TYPES:
            raise WriteRefused(f"match_type for '{text}' must be EXACT, PHRASE or BROAD.")
        if (text.lower(), match) not in seen:
            seen.add((text.lower(), match))
            out.append((text, match))
    return out


# ---- add ---------------------------------------------------------------------


def plan_add_negative_keywords(ads: Ads, cid: str, login: str | None, level: str, campaign_id: str | None,
                               ad_group_id: str | None, shared_set_id: str | None, keywords: list[dict],
                               limit: int) -> Plan:
    wanted = _clean_keywords(keywords, limit)
    target = resolve_target(ads, cid, login, level, campaign_id, ad_group_id, shared_set_id)
    existing = existing_negatives(ads, cid, login, target)

    service, method, operation = {
        "campaign": ("CampaignCriterionService", "mutate_campaign_criteria", "CampaignCriterionOperation"),
        "ad_group": ("AdGroupCriterionService", "mutate_ad_group_criteria", "AdGroupCriterionOperation"),
    }.get(level, ("SharedCriterionService", "mutate_shared_criteria", "SharedCriterionOperation"))
    plan = Plan(service, method, partial_failure=True)

    new = [(t, m) for t, m in wanted if (t.lower(), m) not in existing]
    skipped = [f"'{t}' [{m}]" for t, m in wanted if (t.lower(), m) in existing]
    if skipped:
        plan.warnings.append(f"Already negative on {target['label']}, skipped: {', '.join(skipped)}.")
    if not new:
        return plan

    client = ads.client(login)
    for text, match in new:
        op = client.get_type(operation)
        if level == "campaign":
            op.create.campaign = target["resource"]
            op.create.negative = True
        elif level == "ad_group":
            op.create.ad_group = target["resource"]
            op.create.negative = True
        else:
            op.create.shared_set = target["resource"]
        op.create.keyword.text = text
        op.create.keyword.match_type = client.enums.KeywordMatchTypeEnum[match]
        plan.operations.append(op)
        plan.changes.append(Change(
            object=f"negative keyword '{text}' [{match}] on {target['label']}",
            field="negative_keyword", old=None, new="added", resource=target["resource"],
            object_type=target["object_type"], object_id=target["id"], name=f"{text} [{match}]",
        ))
    plan.warnings.extend(conflicts(ads, cid, login, target, new))
    return plan


# ---- remove ------------------------------------------------------------------


def plan_remove_negative_keywords(ads: Ads, cid: str, login: str | None, level: str, campaign_id: str | None,
                                  ad_group_id: str | None, shared_set_id: str | None, keywords: list[dict],
                                  limit: int) -> Plan:
    wanted = _clean_keywords(keywords, limit)
    target = resolve_target(ads, cid, login, level, campaign_id, ad_group_id, shared_set_id)
    existing = existing_negatives(ads, cid, login, target)

    service, method, operation = {
        "campaign": ("CampaignCriterionService", "mutate_campaign_criteria", "CampaignCriterionOperation"),
        "ad_group": ("AdGroupCriterionService", "mutate_ad_group_criteria", "AdGroupCriterionOperation"),
    }.get(level, ("SharedCriterionService", "mutate_shared_criteria", "SharedCriterionOperation"))
    plan = Plan(service, method, partial_failure=True)

    missing = [f"'{t}' [{m}]" for t, m in wanted if (t.lower(), m) not in existing]
    if missing:
        plan.warnings.append(f"Not a negative keyword on {target['label']}, nothing to remove: {', '.join(missing)}.")

    client = ads.client(login)
    for text, match in wanted:
        resource = existing.get((text.lower(), match))
        if not resource:
            continue
        op = client.get_type(operation)
        op.remove = resource
        plan.operations.append(op)
        plan.changes.append(Change(
            object=f"negative keyword '{text}' [{match}] on {target['label']}",
            field="negative_keyword", old="present", new="removed", resource=resource,
            object_type=target["object_type"], object_id=target["id"], name=f"{text} [{match}]",
        ))
    if plan.changes:
        plan.warnings.append("Searches these negatives blocked can trigger your ads again.")
    return plan


# ---- lists -------------------------------------------------------------------


def plan_manage_negative_keyword_list(ads: Ads, cid: str, login: str | None, action: str, name: str | None,
                                      shared_set_id: str | None, campaign_id: str | None) -> Plan:
    client = ads.client(login)

    if action == "create":
        name = " ".join(str(name or "").split())
        if not name:
            raise WriteRefused("action 'create' needs a name for the new list.")
        if len(name) > 255:
            raise WriteRefused("The list name is too long (max 255 characters).")
        rows, _ = ads.search(cid, "SELECT shared_set.name FROM shared_set WHERE shared_set.type = NEGATIVE_KEYWORDS "
                             "AND shared_set.status = 'ENABLED'", login_customer_id=login, max_rows=10000)
        if any(r["shared_set.name"].lower() == name.lower() for r in rows):
            raise WriteRefused(f"A negative keyword list named '{name}' already exists.")
        plan = Plan("SharedSetService", "mutate_shared_sets")
        op = client.get_type("SharedSetOperation")
        op.create.name = name
        op.create.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
        plan.operations.append(op)
        plan.changes.append(Change(object=f"negative keyword list '{name}'", field="list", old=None, new="created",
                                   resource=f"customers/{cid}", object_type="negative_keyword_list", name=name))
        plan.warnings.append("The new list is empty and not used by any campaign yet. Add keywords with "
                             "add_negative_keywords (level shared_list) and attach it with action 'attach'.")
        return plan

    if action not in ("attach", "detach"):
        raise WriteRefused("action must be create, attach or detach. Deleting a list is not supported.")

    shared_set_id = _need_id(shared_set_id, "shared_set_id", action)
    campaign_id = _need_id(campaign_id, "campaign_id", action)
    lists, _ = ads.search(cid, "SELECT shared_set.resource_name, shared_set.name, shared_set.type, shared_set.status, "
                          f"shared_set.member_count FROM shared_set WHERE shared_set.id = {shared_set_id}",
                          login_customer_id=login, max_rows=1)
    if not lists or lists[0]["shared_set.status"] != "ENABLED" or lists[0]["shared_set.type"] != "NEGATIVE_KEYWORDS":
        raise WriteRefused(f"Negative keyword list {shared_set_id} not found (or removed, or not a negative "
                           f"keyword list) in account {format_customer_id(cid)}.")
    campaigns, _ = ads.search(cid, "SELECT campaign.resource_name, campaign.name, campaign.status, "
                              f"campaign.advertising_channel_type FROM campaign WHERE campaign.id = {campaign_id}",
                              login_customer_id=login, max_rows=1)
    if not campaigns or campaigns[0]["campaign.status"] == "REMOVED":
        raise WriteRefused(f"Campaign {campaign_id} not found (or removed) in account {format_customer_id(cid)}.")
    lst, camp = lists[0], campaigns[0]
    linked, _ = ads.search(cid, "SELECT campaign_shared_set.resource_name FROM campaign_shared_set WHERE "
                           f"campaign.id = {campaign_id} AND shared_set.id = {shared_set_id} AND "
                           "campaign_shared_set.status = 'ENABLED'", login_customer_id=login, max_rows=1)

    label = f"list '{lst['shared_set.name']}' ({shared_set_id}) on campaign '{camp['campaign.name']}' ({campaign_id})"
    plan = Plan("CampaignSharedSetService", "mutate_campaign_shared_sets")
    op = client.get_type("CampaignSharedSetOperation")
    if action == "attach":
        if linked:
            plan.warnings.append(f"The {label} is already attached.")
            return plan
        op.create.campaign = camp["campaign.resource_name"]
        op.create.shared_set = lst["shared_set.resource_name"]
        old, new = "not attached", "attached"
        plan.warnings.append(f"Its {lst['shared_set.member_count']} negative keywords now apply to this campaign.")
    else:
        if not linked:
            plan.warnings.append(f"The {label} is not attached, nothing to detach.")
            return plan
        op.remove = linked[0]["campaign_shared_set.resource_name"]
        old, new = "attached", "detached"
        plan.warnings.append("The list stays as it is. Its negatives no longer apply to this campaign.")
    plan.operations.append(op)
    plan.changes.append(Change(object=label, field="negative_keyword_list", old=old, new=new,
                               resource=camp["campaign.resource_name"], object_type="campaign_negative_keyword_list",
                               object_id=campaign_id, name=f"{lst['shared_set.name']} ({shared_set_id})"))
    return plan
