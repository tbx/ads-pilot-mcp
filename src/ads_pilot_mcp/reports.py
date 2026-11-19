# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only helpers and ready-made reports, so Claude does not have to build GAQL every time.

- describe_resource: which fields, segments and metrics a resource has (GoogleAdsFieldService).
- money: every *_micros value also as a normal amount, plus the account currency.
- field hints: when a GAQL query fails on an unknown field, suggest similar valid fields.
- campaign_overview, search_terms_report, pmax_channel_report, targeting_overview.
"""

from __future__ import annotations

import difflib
import re
from datetime import date
from typing import Any

from .ads import Ads, AdsError

DATE_RANGES = ("TODAY", "YESTERDAY", "LAST_7_DAYS", "LAST_14_DAYS", "LAST_30_DAYS", "THIS_MONTH", "LAST_MONTH")
NETWORK_LABELS = {"SEARCH": "Google Search", "SEARCH_PARTNERS": "Search partners", "CONTENT": "Display",
                  "YOUTUBE": "YouTube", "DISCOVER": "Discover", "MAPS": "Maps", "GMAIL": "Gmail",
                  "GOOGLE_TV": "Google TV", "GOOGLE_OWNED_CHANNELS": "Google owned channels",
                  "MIXED": "Mixed (cross-network)"}


class ReportError(Exception):
    """Invalid report input."""


# ---- helpers -----------------------------------------------------------------


def date_filter(date_range: str | None, start_date: str | None, end_date: str | None) -> tuple[str, str]:
    """GAQL condition for the period, and a readable label."""
    if start_date or end_date:
        if not (start_date and end_date):
            raise ReportError("Give both start_date and end_date (YYYY-MM-DD), or a date_range.")
        for d in (start_date, end_date):
            try:
                date.fromisoformat(d)
            except ValueError:
                raise ReportError(f"'{d}' is not a date in the form YYYY-MM-DD.")
        return f"segments.date BETWEEN '{start_date}' AND '{end_date}'", f"{start_date} to {end_date}"
    period = (date_range or "LAST_30_DAYS").upper()
    if period not in DATE_RANGES:
        raise ReportError(f"date_range must be one of: {', '.join(DATE_RANGES)}.")
    return f"segments.date DURING {period}", period


def add_money(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add 'metrics.cost' next to 'metrics.cost_micros' (and so on for every *_micros value)."""
    for row in rows:
        for key in [k for k in row if k.endswith("_micros")]:
            value = row[key]
            if isinstance(value, (int, float)):
                row[key[: -len("_micros")]] = round(value / 1_000_000, 2)
    return rows


def _id(value: str | None, name: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    if not re.fullmatch(r"\d+", str(value).strip()):
        raise ReportError(f"{name} must be a number.")
    return str(value).strip()


def _ratio(a: float, b: float, digits: int = 2) -> float | None:
    return round(a / b, digits) if b else None


# ---- describe_resource and field hints ---------------------------------------


def describe_resource(ads: Ads, resource: str, field_name: str | None = None) -> dict[str, Any]:
    resource = resource.strip().lower()
    if not re.fullmatch(r"[a-z_]+", resource):
        raise ReportError("resource must be a resource name like 'campaign' or 'search_term_view'.")
    if field_name:
        if not re.fullmatch(r"[a-z_.]+", field_name):
            raise ReportError("field must look like 'segments.ad_network_type'.")
        rows = ads.search_fields("SELECT name, category, data_type, selectable, filterable, sortable, is_repeated, "
                                 f"enum_values, selectable_with WHERE name = '{field_name}'")
        if not rows:
            raise ReportError(f"No field '{field_name}' in this API version.")
        f = rows[0]
        return {"field": f["name"], "data_type": f["data_type"], "filterable": f["filterable"],
                "sortable": f["sortable"], "enum_values": f["enum_values"]}
    main = ads.search_fields("SELECT name, category, selectable_with WHERE name = '" + resource + "'")
    if not main or main[0]["category"] != "RESOURCE":
        raise ReportError(f"'{resource}' is not a resource in this API version.")
    attributes = ads.search_fields("SELECT name, category, data_type, selectable, filterable, sortable, is_repeated, "
                                   f"enum_values WHERE name LIKE '{resource}.%'")
    compatible = main[0]["selectable_with"]
    return {
        "resource": resource,
        "fields": [{"name": f["name"], "type": f["data_type"], "filterable": f["filterable"],
                    **({"enum_values": f["enum_values"]} if f["enum_values"] else {})}
                   for f in attributes if f["selectable"]],
        "segments": sorted(n for n in compatible if n.startswith("segments.")),
        "metrics": sorted(n for n in compatible if n.startswith("metrics.")),
        "attributed_resources": sorted(n for n in compatible if "." not in n),
        "hint": "For the valid values of one segment or field, call describe_resource with field='segments.xyz'.",
    }


def field_hints(ads: Ads, error: str) -> list[str]:
    """Suggest valid fields close to the unknown ones named in a GAQL error."""
    names = set(re.findall(r"'([a-z_]+\.[a-z_.]+)'", error))
    if not names or not re.search(r"UNRECOGNIZED_FIELD|INVALID_FIELD|unrecognized|not found", error, re.I):
        return []
    hints = []
    for name in sorted(names):
        prefix = name.split(".", 1)[0]
        try:
            known = [f["name"] for f in ads.search_fields(f"SELECT name WHERE name LIKE '{prefix}.%'")]
        except AdsError:
            continue
        close = difflib.get_close_matches(name, known, n=5, cutoff=0.5)
        if close:
            hints.append(f"'{name}' does not exist in this API version. Similar fields: {', '.join(close)}.")
        elif known:
            hints.append(f"'{name}' does not exist in this API version. See describe_resource('{prefix}').")
    return hints


# ---- reports -----------------------------------------------------------------


def campaign_overview(ads: Ads, cid: str, login: str | None, date_range: str | None, start_date: str | None,
                      end_date: str | None, include_paused: bool) -> dict[str, Any]:
    period, label = date_filter(date_range, start_date, end_date)
    statuses = "('ENABLED', 'PAUSED')" if include_paused else "('ENABLED')"
    rows, _ = ads.search(cid, "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, campaign.status, "
                         "campaign.bidding_strategy_type, campaign_budget.amount_micros, metrics.cost_micros, "
                         "metrics.clicks, metrics.impressions, metrics.conversions, metrics.conversions_value "
                         f"FROM campaign WHERE campaign.status IN {statuses} AND {period} "
                         "ORDER BY metrics.cost_micros DESC", login_customer_id=login, max_rows=1000)
    out, total = [], {"cost": 0.0, "clicks": 0, "impressions": 0, "conversions": 0.0, "conversion_value": 0.0}
    for r in rows:
        cost = (r["metrics.cost_micros"] or 0) / 1_000_000
        conv, value = r["metrics.conversions"] or 0, r["metrics.conversions_value"] or 0
        out.append({
            "campaign_id": str(r["campaign.id"]), "name": r["campaign.name"],
            "type": r["campaign.advertising_channel_type"], "status": r["campaign.status"],
            "bidding": r["campaign.bidding_strategy_type"],
            "daily_budget": round((r["campaign_budget.amount_micros"] or 0) / 1_000_000, 2),
            "cost": round(cost, 2), "clicks": r["metrics.clicks"], "impressions": r["metrics.impressions"],
            "conversions": round(conv, 2), "cost_per_conversion": _ratio(cost, conv),
            "conversion_value": round(value, 2), "roas": _ratio(value, cost),
        })
        total["cost"] += cost
        total["clicks"] += r["metrics.clicks"] or 0
        total["impressions"] += r["metrics.impressions"] or 0
        total["conversions"] += conv
        total["conversion_value"] += value
    total = {k: round(v, 2) if isinstance(v, float) else v for k, v in total.items()}
    total["cost_per_conversion"] = _ratio(total["cost"], total["conversions"])
    return {"period": label, "currency": ads.currency(cid, login), "campaigns": out, "total": total,
            "note": "Campaigns without any impressions in the period are not listed."}


def search_terms_report(ads: Ads, cid: str, login: str | None, campaign_id: str | None, date_range: str | None,
                        start_date: str | None, end_date: str | None, sort_by: str,
                        min_cost_without_conversion: float | None, limit: int) -> dict[str, Any]:
    period, label = date_filter(date_range, start_date, end_date)
    campaign_id = _id(campaign_id, "campaign_id")
    only = f" AND campaign.id = {campaign_id}" if campaign_id else ""
    metrics = "metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, metrics.conversions_value"
    terms: list[dict[str, Any]] = []
    search_rows, _ = ads.search(cid, "SELECT search_term_view.search_term, search_term_view.status, campaign.id, "
                                f"campaign.name, ad_group.name, {metrics} FROM search_term_view WHERE {period}{only}",
                                login_customer_id=login, max_rows=10000)
    for r in search_rows:
        terms.append({"search_term": r["search_term_view.search_term"], "source": "Search",
                      "campaign": r["campaign.name"], "campaign_id": str(r["campaign.id"]),
                      "ad_group": r["ad_group.name"], "status": r["search_term_view.status"], **_metrics(r)})
    try:
        pmax_rows, _ = ads.search(cid, "SELECT campaign_search_term_view.search_term, campaign.id, campaign.name, "
                                  f"{metrics} FROM campaign_search_term_view WHERE {period} AND "
                                  f"campaign.advertising_channel_type = PERFORMANCE_MAX{only}",
                                  login_customer_id=login, max_rows=10000)
    except AdsError:
        pmax_rows = []
    for r in pmax_rows:
        terms.append({"search_term": r["campaign_search_term_view.search_term"], "source": "Performance Max",
                      "campaign": r["campaign.name"], "campaign_id": str(r["campaign.id"]), **_metrics(r)})
    if min_cost_without_conversion is not None:
        terms = [t for t in terms if t["cost"] >= min_cost_without_conversion and not t["conversions"]]
    key = {"cost": "cost", "conversions": "conversions", "clicks": "clicks"}.get(sort_by)
    if not key:
        raise ReportError("sort_by must be cost, conversions or clicks.")
    terms.sort(key=lambda t: (t[key] or 0), reverse=True)
    limit = max(1, min(int(limit), 1000))
    return {"period": label, "currency": ads.currency(cid, login), "count": len(terms), "truncated": len(terms) > limit,
            "search_terms": terms[:limit],
            "note": "Performance Max search terms come from campaign_search_term_view; Google hides low-volume terms."}


def _metrics(r: dict) -> dict[str, Any]:
    cost = (r["metrics.cost_micros"] or 0) / 1_000_000
    conv = r["metrics.conversions"] or 0
    return {"cost": round(cost, 2), "clicks": r["metrics.clicks"], "impressions": r["metrics.impressions"],
            "conversions": round(conv, 2), "cost_per_conversion": _ratio(cost, conv),
            "conversion_value": round(r["metrics.conversions_value"] or 0, 2)}


def pmax_channel_report(ads: Ads, cid: str, login: str | None, campaign_id: str | None, date_range: str | None,
                        start_date: str | None, end_date: str | None) -> dict[str, Any]:
    period, label = date_filter(date_range, start_date, end_date)
    campaign_id = _id(campaign_id, "campaign_id")
    where = f"campaign.advertising_channel_type = PERFORMANCE_MAX AND {period}"
    if campaign_id:
        where += f" AND campaign.id = {campaign_id}"
    rows, _ = ads.search(cid, "SELECT campaign.id, campaign.name, segments.ad_network_type, metrics.cost_micros, "
                         "metrics.clicks, metrics.impressions, metrics.conversions, metrics.conversions_value "
                         f"FROM campaign WHERE {where}", login_customer_id=login, max_rows=10000)
    by_action, _ = ads.search(cid, "SELECT campaign.id, segments.ad_network_type, segments.conversion_action_name, "
                              f"metrics.conversions, metrics.conversions_value FROM campaign WHERE {where}",
                              login_customer_id=login, max_rows=10000)
    try:
        formats, _ = ads.search(cid, "SELECT campaign.id, segments.ad_format_type, metrics.impressions, metrics.cost_micros "
                                f"FROM campaign WHERE {where}", login_customer_id=login, max_rows=1000)
    except AdsError:
        formats = []

    campaigns: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = campaigns.setdefault(str(r["campaign.id"]), {"campaign_id": str(r["campaign.id"]), "name": r["campaign.name"],
                                                          "channels": {}, "formats": {}})
        channel = NETWORK_LABELS.get(r["segments.ad_network_type"], r["segments.ad_network_type"])
        c["channels"][channel] = {**_metrics(r), "conversions_by_action": {}}
    for r in by_action:
        c = campaigns.get(str(r["campaign.id"]))
        channel = NETWORK_LABELS.get(r["segments.ad_network_type"], r["segments.ad_network_type"])
        if c and channel in c["channels"] and r["metrics.conversions"]:
            c["channels"][channel]["conversions_by_action"][r["segments.conversion_action_name"]] = round(
                r["metrics.conversions"], 2)
    for r in formats:
        c = campaigns.get(str(r["campaign.id"]))
        if c:
            c["formats"][r["segments.ad_format_type"]] = {"impressions": r["metrics.impressions"],
                                                          "cost": round((r["metrics.cost_micros"] or 0) / 1_000_000, 2)}
    note = ("Channels come from segments.ad_network_type. Google cannot combine it with the ad format in one query, "
            "so 'formats' is a separate split per campaign; for Performance Max it usually only shows OTHER, i.e. the "
            "API gives no split such as local versus text ads.")
    return {"period": label, "currency": ads.currency(cid, login), "campaigns": list(campaigns.values()), "note": note}


def targeting_overview(ads: Ads, cid: str, login: str | None, campaign_id: str | None,
                       include_paused: bool) -> dict[str, Any]:
    campaign_id = _id(campaign_id, "campaign_id")
    statuses = "('ENABLED', 'PAUSED')" if include_paused else "('ENABLED')"
    only = f" AND campaign.id = {campaign_id}" if campaign_id else ""
    camps, _ = ads.search(cid, "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
                          "campaign.geo_target_type_setting.positive_geo_target_type, "
                          "campaign.network_settings.target_search_network, campaign.network_settings.target_content_network "
                          f"FROM campaign WHERE campaign.status IN {statuses}{only}", login_customer_id=login, max_rows=1000)
    if not camps:
        return {"campaigns": [], "warnings": []}
    ids = ", ".join(str(c["campaign.id"]) for c in camps)
    crits, _ = ads.search(cid, "SELECT campaign.id, campaign_criterion.type, campaign_criterion.negative, "
                          "campaign_criterion.location.geo_target_constant, campaign_criterion.language.language_constant "
                          f"FROM campaign_criterion WHERE campaign.id IN ({ids}) AND campaign_criterion.type IN "
                          "(LOCATION, PROXIMITY, LOCATION_GROUP, LANGUAGE) AND campaign_criterion.status != 'REMOVED'",
                          login_customer_id=login, max_rows=20000)
    geo_ids = sorted({r["campaign_criterion.location.geo_target_constant"].rsplit("/", 1)[-1]
                      for r in crits if r.get("campaign_criterion.location.geo_target_constant")})
    geo_names: dict[str, str] = {}
    for i in range(0, len(geo_ids), 500):
        chunk = geo_ids[i:i + 500]
        rows, _ = ads.search(cid, "SELECT geo_target_constant.id, geo_target_constant.canonical_name, "
                             "geo_target_constant.target_type FROM geo_target_constant WHERE geo_target_constant.id IN "
                             f"({', '.join(chunk)})", login_customer_id=login, max_rows=len(chunk))
        geo_names.update({str(r["geo_target_constant.id"]):
                          f"{r['geo_target_constant.canonical_name']} ({r['geo_target_constant.target_type']})" for r in rows})
    langs, _ = ads.search(cid, "SELECT language_constant.id, language_constant.name FROM language_constant",
                          login_customer_id=login, max_rows=1000)
    lang_names = {str(r["language_constant.id"]): r["language_constant.name"] for r in langs}
    try:
        asset_sets, _ = ads.search(cid, "SELECT campaign.id, asset_set.type, asset_set.name FROM campaign_asset_set "
                                   f"WHERE campaign.id IN ({ids}) AND campaign_asset_set.status = 'ENABLED'",
                                   login_customer_id=login, max_rows=5000)
        account_sets, _ = ads.search(cid, "SELECT asset_set.type, asset_set.name FROM customer_asset_set "
                                     "WHERE customer_asset_set.status = 'ENABLED'", login_customer_id=login, max_rows=500)
    except AdsError:
        asset_sets, account_sets = [], []
    account_locations = [f"{r['asset_set.name']} ({r['asset_set.type']})" for r in account_sets
                         if "LOCATION" in (r.get("asset_set.type") or "")]

    out, warnings = [], []
    for c in camps:
        cid_ = c["campaign.id"]
        mine = [r for r in crits if r["campaign.id"] == cid_]
        targeted = [geo_names.get(r["campaign_criterion.location.geo_target_constant"].rsplit("/", 1)[-1], "?")
                    for r in mine if r["campaign_criterion.type"] == "LOCATION" and not r["campaign_criterion.negative"]]
        excluded = [geo_names.get(r["campaign_criterion.location.geo_target_constant"].rsplit("/", 1)[-1], "?")
                    for r in mine if r["campaign_criterion.type"] == "LOCATION" and r["campaign_criterion.negative"]]
        radius = sum(1 for r in mine if r["campaign_criterion.type"] in ("PROXIMITY", "LOCATION_GROUP")
                     and not r["campaign_criterion.negative"])
        languages = [lang_names.get(r["campaign_criterion.language.language_constant"].rsplit("/", 1)[-1], "?")
                     for r in mine if r["campaign_criterion.type"] == "LANGUAGE"]
        is_search = c["campaign.advertising_channel_type"] == "SEARCH"
        item = {
            "campaign_id": str(cid_), "name": c["campaign.name"], "type": c["campaign.advertising_channel_type"],
            "status": c["campaign.status"],
            "location_option": c["campaign.geo_target_type_setting.positive_geo_target_type"],
            "locations_targeted": targeted, "locations_excluded": excluded, "radius_or_location_groups": radius,
            "languages": languages or ["all languages"],
            "networks": ({"search_partners": c["campaign.network_settings.target_search_network"],
                          "display": c["campaign.network_settings.target_content_network"]}
                         if is_search else "all Google networks (not configurable)"
                         if c["campaign.advertising_channel_type"] == "PERFORMANCE_MAX" else None),
            "location_assets": [f"{r['asset_set.name']} ({r['asset_set.type']})" for r in asset_sets
                                if r["campaign.id"] == cid_ and "LOCATION" in (r.get("asset_set.type") or "")],
        }
        if not targeted and not radius:
            item["warning"] = "No location targeting: this campaign shows ads WORLDWIDE."
            warnings.append(f"Campaign '{c['campaign.name']}' ({cid_}) has no location targeting and runs worldwide.")
        if not languages:
            warnings.append(f"Campaign '{c['campaign.name']}' ({cid_}) targets all languages.")
        out.append(item)
    return {"campaigns": out, "account_location_assets": account_locations, "warnings": warnings}
