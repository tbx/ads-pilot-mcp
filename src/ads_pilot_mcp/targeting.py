# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Targeting: locations, languages and networks of a campaign.

- Locations: CampaignCriterion of type LOCATION (positive = targeted, negative = excluded),
  plus campaign.geo_target_type_setting.positive_geo_target_type (presence or presence-or-interest).
  Sent as one GoogleAdsService.mutate request, so criteria and the setting change together or not at all.
- Languages: CampaignCriterion of type LANGUAGE.
- Networks: campaign.network_settings, only for Search campaigns.

Same rules as every write: dry run first, confirm token, allowlist, change log.
Removing a location or language is allowed: it is a targeting change, nothing is deleted from the account.
"""

from __future__ import annotations

import re
from typing import Any

from .ads import Ads
from .config import format_customer_id
from .writes import MAX_ITEMS, Change, Plan, WriteRefused, _mask

GEO_LABELS = {"PRESENCE": "presence (people in the location)",
              "PRESENCE_OR_INTEREST": "presence or interest (people in or interested in the location)",
              "SEARCH_INTEREST": "search interest"}
POSITIVE_TYPES = ("LOCATION", "PROXIMITY", "LOCATION_GROUP")


def _campaign(ads: Ads, cid: str, login: str | None, campaign_id: str, extra: str = "") -> dict[str, Any]:
    if not re.fullmatch(r"\d+", str(campaign_id).strip()):
        raise WriteRefused("campaign_id must be a number.")
    rows, _ = ads.search(cid, "SELECT campaign.resource_name, campaign.name, campaign.status, "
                         f"campaign.advertising_channel_type{extra} FROM campaign WHERE campaign.id = {campaign_id}",
                         login_customer_id=login, max_rows=1)
    if not rows or rows[0]["campaign.status"] == "REMOVED":
        raise WriteRefused(f"Campaign {campaign_id} not found (or removed) in account {format_customer_id(cid)}.")
    return rows[0]


def _label(row: dict) -> str:
    return f"campaign '{row['campaign.name']}' ({row['campaign.advertising_channel_type']})"


# ---- locations ---------------------------------------------------------------


def resolve_locations(ads: Ads, cid: str, login: str | None, entries: list[str],
                      country_code: str | None) -> dict[str, dict[str, Any]]:
    """Map each input (geo target ID or name) to one geo target constant. Ambiguous names are refused."""
    out: dict[str, dict[str, Any]] = {}
    ids = [e for e in entries if re.fullmatch(r"\d+", e)]
    names = [e for e in entries if not re.fullmatch(r"\d+", e)]
    if ids:
        rows, _ = ads.search(cid, "SELECT geo_target_constant.id, geo_target_constant.name, "
                             "geo_target_constant.canonical_name, geo_target_constant.target_type, "
                             "geo_target_constant.status FROM geo_target_constant WHERE geo_target_constant.id IN "
                             f"({', '.join(ids)})", login_customer_id=login, max_rows=len(ids))
        found = {str(r["geo_target_constant.id"]): r for r in rows}
        missing = [i for i in ids if i not in found]
        if missing:
            raise WriteRefused(f"Unknown geo target ID(s): {', '.join(missing)}.")
        for i in ids:
            r = found[i]
            out[i] = {"id": i, "name": r["geo_target_constant.name"], "canonical_name": r["geo_target_constant.canonical_name"],
                      "type": r["geo_target_constant.target_type"], "status": r["geo_target_constant.status"]}
    if names:
        suggestions = ads.suggest_geo_targets(names, country_code)
        for name in names:
            candidates = suggestions.get(name, [])
            exact = [c for c in candidates if name.lower() in (c["name"].lower(), c["canonical_name"].lower())]
            pick = exact if len(exact) == 1 else candidates if len(candidates) == 1 else []
            if not pick:
                if not candidates:
                    raise WriteRefused(f"No location found for '{name}'. Check the spelling or use the geo target ID.")
                options = "; ".join(f"{c['id']} = {c['canonical_name']} ({c['type']})" for c in (exact or candidates)[:6])
                raise WriteRefused(f"'{name}' is ambiguous. Use one of these IDs, or add country_code: {options}.")
            out[name] = pick[0]
    return out


def plan_location_targeting(ads: Ads, cid: str, login: str | None, campaign_id: str, add: list[str],
                            exclude: list[str], remove: list[str], country_code: str | None,
                            location_type: str | None, allowed: bool) -> Plan:
    if not allowed:
        raise WriteRefused("This profile does not allow location changes (allow_location_changes: false).")
    add, exclude, remove = ([" ".join(str(x).split()) for x in (lst or []) if str(x).strip()]
                            for lst in (add, exclude, remove))
    if not (add or exclude or remove or location_type):
        raise WriteRefused("Give locations to add, exclude or remove, or a location_type.")
    if len(add) + len(exclude) + len(remove) > MAX_ITEMS:
        raise WriteRefused(f"At most {MAX_ITEMS} locations per call.")
    if location_type and location_type not in ("PRESENCE", "PRESENCE_OR_INTEREST"):
        raise WriteRefused("location_type must be PRESENCE or PRESENCE_OR_INTEREST.")

    row = _campaign(ads, cid, login, campaign_id, ", campaign.geo_target_type_setting.positive_geo_target_type")
    label = _label(row)
    geo = resolve_locations(ads, cid, login, add + exclude + remove, country_code)

    rows, _ = ads.search(cid, "SELECT campaign_criterion.resource_name, campaign_criterion.type, "
                         "campaign_criterion.negative, campaign_criterion.location.geo_target_constant "
                         f"FROM campaign_criterion WHERE campaign.id = {campaign_id} AND campaign_criterion.type IN "
                         "(LOCATION, PROXIMITY, LOCATION_GROUP) AND campaign_criterion.status != 'REMOVED'",
                         login_customer_id=login, max_rows=10000)
    existing = {r["campaign_criterion.location.geo_target_constant"].rsplit("/", 1)[-1]:
                (r["campaign_criterion.resource_name"], bool(r["campaign_criterion.negative"]))
                for r in rows if r["campaign_criterion.type"] == "LOCATION"}
    other_positive = sum(1 for r in rows if r["campaign_criterion.type"] != "LOCATION" and not r["campaign_criterion.negative"])
    positive_now = {g for g, (_, neg) in existing.items() if not neg}

    client = ads.client(login)
    plan = Plan("GoogleAdsService", "mutate")

    def describe(g: dict) -> str:
        return f"location '{g['canonical_name']}' ({g['type']}, {g['id']}) in {label}"

    def add_criterion(g: dict, negative: bool, old: str, new: str) -> None:
        op = client.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.create
        crit.campaign = row["campaign.resource_name"]
        crit.location.geo_target_constant = f"geoTargetConstants/{g['id']}"
        crit.negative = negative
        plan.operations.append(op)
        plan.changes.append(Change(describe(g), "location_targeting", old, new, row["campaign.resource_name"],
                                   object_type="campaign_location", object_id=str(campaign_id), name=g["canonical_name"]))

    added, removed = set(), set()
    for key in add:
        g = geo[key]
        if g["id"] in existing:
            if existing[g["id"]][1]:
                raise WriteRefused(f"{describe(g)} is currently excluded. Remove the exclusion first (remove), "
                                   "then add it in a second call.")
            plan.warnings.append(f"{describe(g)} is already targeted, skipped.")
            continue
        add_criterion(g, False, "not targeted", "targeted")
        added.add(g["id"])
    for key in exclude:
        g = geo[key]
        if g["id"] in existing:
            if not existing[g["id"]][1]:
                raise WriteRefused(f"{describe(g)} is currently targeted. Remove it first (remove), then exclude it "
                                   "in a second call.")
            plan.warnings.append(f"{describe(g)} is already excluded, skipped.")
            continue
        add_criterion(g, True, "not excluded", "excluded")
    for key in remove:
        g = geo[key]
        if g["id"] not in existing:
            plan.warnings.append(f"{describe(g)} is neither targeted nor excluded, nothing to remove.")
            continue
        resource, negative = existing[g["id"]]
        op = client.get_type("MutateOperation")
        op.campaign_criterion_operation.remove = resource
        plan.operations.append(op)
        plan.changes.append(Change(describe(g), "location_targeting", "excluded" if negative else "targeted",
                                   "removed", resource, object_type="campaign_location", object_id=str(campaign_id),
                                   name=g["canonical_name"]))
        if not negative:
            removed.add(g["id"])

    for g in geo.values():
        if g.get("status") == "REMOVAL_PLANNED":
            plan.warnings.append(f"Google plans to retire {describe(g)}. Prefer another location.")

    current_type = row["campaign.geo_target_type_setting.positive_geo_target_type"]
    if location_type and location_type != current_type:
        op = client.get_type("MutateOperation")
        op.campaign_operation.update.resource_name = row["campaign.resource_name"]
        op.campaign_operation.update.geo_target_type_setting.positive_geo_target_type = \
            client.enums.PositiveGeoTargetTypeEnum[location_type]
        client.copy_from(op.campaign_operation.update_mask, _mask("geo_target_type_setting.positive_geo_target_type"))
        plan.operations.append(op)
        plan.changes.append(Change(f"location option of {label}", "positive_geo_target_type",
                                   GEO_LABELS.get(current_type, current_type), GEO_LABELS[location_type],
                                   row["campaign.resource_name"], object_type="campaign_location_option",
                                   object_id=str(campaign_id), name=row["campaign.name"]))

    positive_after = len((positive_now - removed) | added) + other_positive
    if positive_after == 0:
        if positive_now or other_positive:
            plan.warnings.insert(0, f"WARNING: after this change {label} has NO location targeting and shows ads "
                                    "WORLDWIDE. Add at least one location if that is not intended.")
        else:
            plan.warnings.insert(0, f"Note: {label} has no location targeting and shows ads worldwide, "
                                    "also after this change.")
    return plan


# ---- languages ---------------------------------------------------------------


def plan_language_targeting(ads: Ads, cid: str, login: str | None, campaign_id: str, add: list[str],
                            remove: list[str]) -> Plan:
    add, remove = ([str(x).strip() for x in (lst or []) if str(x).strip()] for lst in (add, remove))
    if not (add or remove):
        raise WriteRefused("Give languages to add or remove.")
    row = _campaign(ads, cid, login, campaign_id)
    label = _label(row)

    langs, _ = ads.search(cid, "SELECT language_constant.id, language_constant.code, language_constant.name, "
                          "language_constant.targetable FROM language_constant", login_customer_id=login, max_rows=1000)
    lookup: dict[str, dict] = {}
    for r in langs:
        for key in (str(r["language_constant.id"]), r["language_constant.code"], r["language_constant.name"]):
            lookup[str(key).lower()] = r

    def resolve(entry: str) -> dict:
        r = lookup.get(entry.lower())
        if not r:
            raise WriteRefused(f"Unknown language '{entry}'. Use an ID (e.g. 1001), a code (e.g. de) or an English "
                               "name (e.g. German).")
        if not r["language_constant.targetable"]:
            raise WriteRefused(f"Language '{r['language_constant.name']}' cannot be targeted in Google Ads.")
        return r

    existing_rows, _ = ads.search(cid, "SELECT campaign_criterion.resource_name, campaign_criterion.language.language_constant "
                                  f"FROM campaign_criterion WHERE campaign.id = {campaign_id} AND campaign_criterion.type = "
                                  "LANGUAGE AND campaign_criterion.status != 'REMOVED'", login_customer_id=login, max_rows=1000)
    existing = {r["campaign_criterion.language.language_constant"].rsplit("/", 1)[-1]: r["campaign_criterion.resource_name"]
                for r in existing_rows}

    client = ads.client(login)
    plan = Plan("CampaignCriterionService", "mutate_campaign_criteria")
    after = set(existing)

    def describe(r: dict) -> str:
        return f"language {r['language_constant.name']} ({r['language_constant.code']}, {r['language_constant.id']}) in {label}"

    for entry in add:
        r = resolve(entry)
        lid = str(r["language_constant.id"])
        if lid in existing:
            plan.warnings.append(f"{describe(r)} is already targeted, skipped.")
            continue
        op = client.get_type("CampaignCriterionOperation")
        op.create.campaign = row["campaign.resource_name"]
        op.create.language.language_constant = f"languageConstants/{lid}"
        plan.operations.append(op)
        plan.changes.append(Change(describe(r), "language_targeting", "not targeted", "targeted",
                                   row["campaign.resource_name"], object_type="campaign_language",
                                   object_id=str(campaign_id), name=r["language_constant.name"]))
        after.add(lid)
    for entry in remove:
        r = resolve(entry)
        lid = str(r["language_constant.id"])
        if lid not in existing:
            plan.warnings.append(f"{describe(r)} is not targeted, nothing to remove.")
            continue
        op = client.get_type("CampaignCriterionOperation")
        op.remove = existing[lid]
        plan.operations.append(op)
        plan.changes.append(Change(describe(r), "language_targeting", "targeted", "removed", existing[lid],
                                   object_type="campaign_language", object_id=str(campaign_id),
                                   name=r["language_constant.name"]))
        after.discard(lid)
    if plan.changes and not after:
        plan.warnings.insert(0, f"WARNING: after this change {label} has no language targeting and shows ads "
                                "to users of ALL languages.")
    return plan


# ---- networks ----------------------------------------------------------------


def plan_network_settings(ads: Ads, cid: str, login: str | None, campaign_id: str, search_partners: bool | None,
                          display_network: bool | None) -> Plan:
    if search_partners is None and display_network is None:
        raise WriteRefused("Give search_partners and/or display_network (true or false).")
    row = _campaign(ads, cid, login, campaign_id, ", campaign.network_settings.target_search_network, "
                    "campaign.network_settings.target_content_network")
    if row["campaign.advertising_channel_type"] != "SEARCH":
        kind = row["campaign.advertising_channel_type"]
        reason = " Performance Max always uses all Google networks; there is no such setting." \
            if kind == "PERFORMANCE_MAX" else ""
        raise WriteRefused(f"{_label(row)} is a {kind} campaign. Network settings can only be changed for Search "
                           f"campaigns.{reason}")
    label = _label(row)
    client = ads.client(login)
    plan = Plan("CampaignService", "mutate_campaigns")
    op = client.get_type("CampaignOperation")
    op.update.resource_name = row["campaign.resource_name"]
    paths = []
    onoff = {True: "on", False: "off", None: "off"}
    for wanted, field, name in ((search_partners, "target_search_network", "Search partners"),
                                (display_network, "target_content_network", "Display network")):
        if wanted is None:
            continue
        current = bool(row[f"campaign.network_settings.{field}"])
        if current == wanted:
            plan.warnings.append(f"{name} in {label} is already {onoff[wanted]}, skipped.")
            continue
        setattr(op.update.network_settings, field, wanted)
        paths.append(f"network_settings.{field}")
        plan.changes.append(Change(f"{name} in {label}", field, onoff[current], onoff[wanted],
                                   row["campaign.resource_name"], object_type="campaign_network",
                                   object_id=str(campaign_id), name=name))
        if wanted and field == "target_content_network":
            plan.warnings.append("With the Display network on, the Search campaign also shows ads on websites "
                                 "and apps. Watch costs and conversion quality.")
    if paths:
        client.copy_from(op.update_mask, _mask(*paths))
        plan.operations.append(op)
    return plan
