# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""set_bidding_strategy: switch the standard bid strategy of one campaign.

Supported (standard strategies on the campaign, never portfolio strategies):
  Search:           MAXIMIZE_CONVERSIONS (optional target_cpa), MAXIMIZE_CONVERSION_VALUE (optional
                    target_roas_percent), TARGET_SPEND = "Maximize clicks" (optional cpc_bid_ceiling),
                    MANUAL_CPC, TARGET_IMPRESSION_SHARE (location, share, cpc_bid_ceiling)
  Performance Max:  MAXIMIZE_CONVERSIONS and MAXIMIZE_CONVERSION_VALUE only

Old and new state are logged as a dict, so revert_change can restore strategy plus values.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .ads import Ads
from .config import Profile, format_customer_id
from .writes import Change, Plan, WriteRefused, _mask

STRATEGIES = ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "TARGET_SPEND", "MANUAL_CPC",
              "TARGET_IMPRESSION_SHARE")
PMAX_STRATEGIES = ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE")
CONVERSION_STRATEGIES = ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE")
LOCATIONS = ("ANYWHERE_ON_PAGE", "TOP_OF_PAGE", "ABSOLUTE_TOP_OF_PAGE")
LABELS = {"MAXIMIZE_CONVERSIONS": "Maximize conversions", "MAXIMIZE_CONVERSION_VALUE": "Maximize conversion value",
          "TARGET_SPEND": "Maximize clicks", "MANUAL_CPC": "Manual CPC", "TARGET_IMPRESSION_SHARE": "Target impression share",
          "TARGET_CPA": "Target CPA", "TARGET_ROAS": "Target ROAS"}
# Field prefixes in change_event.changed_fields that mean "bidding changed".
BIDDING_FIELDS = ("maximize_conversions", "maximize_conversion_value", "target_spend", "manual_cpc", "target_cpa",
                  "target_roas", "target_impression_share", "bidding_strategy")
MIN_CONVERSIONS = 15
LEARNING_DAYS = 14

READ_FIELDS = ("campaign.maximize_conversions.target_cpa_micros", "campaign.maximize_conversion_value.target_roas",
               "campaign.target_spend.cpc_bid_ceiling_micros", "campaign.target_cpa.target_cpa_micros",
               "campaign.target_roas.target_roas", "campaign.target_impression_share.location",
               "campaign.target_impression_share.location_fraction_micros",
               "campaign.target_impression_share.cpc_bid_ceiling_micros")


def _money(micros: int | None) -> float | None:
    return round(micros / 1_000_000, 2) if micros else None


def _micros(amount: float) -> int:
    return int(round(amount * 100)) * 10_000


def current_state(row: dict[str, Any]) -> dict[str, Any]:
    """The campaign's strategy with its values, as logged and shown in the dry run."""
    strategy = row["campaign.bidding_strategy_type"]
    state: dict[str, Any] = {"strategy": strategy}
    if strategy == "MAXIMIZE_CONVERSIONS":
        state["target_cpa"] = _money(row.get("campaign.maximize_conversions.target_cpa_micros"))
    elif strategy == "TARGET_CPA":
        state["target_cpa"] = _money(row.get("campaign.target_cpa.target_cpa_micros"))
    elif strategy == "MAXIMIZE_CONVERSION_VALUE":
        roas = row.get("campaign.maximize_conversion_value.target_roas")
        state["target_roas_percent"] = round(roas * 100, 1) if roas else None
    elif strategy == "TARGET_ROAS":
        roas = row.get("campaign.target_roas.target_roas")
        state["target_roas_percent"] = round(roas * 100, 1) if roas else None
    elif strategy == "TARGET_SPEND":
        state["cpc_bid_ceiling"] = _money(row.get("campaign.target_spend.cpc_bid_ceiling_micros"))
    elif strategy == "TARGET_IMPRESSION_SHARE":
        fraction = row.get("campaign.target_impression_share.location_fraction_micros")
        state["location"] = row.get("campaign.target_impression_share.location")
        state["impression_share_percent"] = round(fraction / 10_000, 1) if fraction else None
        state["cpc_bid_ceiling"] = _money(row.get("campaign.target_impression_share.cpc_bid_ceiling_micros"))
    return state


def describe(state: dict[str, Any], currency: str) -> str:
    """'Maximize clicks, max. CPC 1.20 EUR' for the dry run."""
    parts = [LABELS.get(state["strategy"], state["strategy"])]
    if state.get("target_cpa"):
        parts.append(f"target CPA {state['target_cpa']:.2f} {currency}")
    if state.get("target_roas_percent"):
        parts.append(f"target ROAS {state['target_roas_percent']:g}%")
    if state.get("location"):
        parts.append(f"{state.get('impression_share_percent') or '?'}% {state['location']}")
    if state.get("cpc_bid_ceiling"):
        parts.append(f"max. CPC {state['cpc_bid_ceiling']:.2f} {currency}")
    elif state["strategy"] in ("TARGET_SPEND", "TARGET_IMPRESSION_SHARE"):
        parts.append("no max. CPC")
    return ", ".join(parts).strip()


def last_bidding_change(ads: Ads, cid: str, login: str | None, campaign_id: str,
                        logged: list[dict[str, Any]]) -> str | None:
    """Date of the newest bidding change of this campaign in the last LEARNING_DAYS days, or None."""
    since = dt.datetime.now() - dt.timedelta(days=LEARNING_DAYS)
    dates = []
    for entry in logged:
        if entry.get("tool") in ("set_bidding_strategy", "set_bidding_target") and \
                str(entry.get("object_id")) == str(campaign_id) and entry.get("time"):
            try:
                when = dt.datetime.fromisoformat(entry["time"]).replace(tzinfo=None)
            except ValueError:
                continue
            if when >= since:
                dates.append(when)
    try:
        rows, _ = ads.search(cid, "SELECT change_event.change_date_time, change_event.changed_fields FROM change_event "
                             "WHERE change_event.change_resource_type = 'CAMPAIGN' "
                             f"AND change_event.campaign = 'customers/{cid}/campaigns/{campaign_id}' "
                             f"AND change_event.change_date_time >= '{since:%Y-%m-%d}' "
                             f"AND change_event.change_date_time <= '{dt.date.today() + dt.timedelta(days=1)}' "
                             "ORDER BY change_event.change_date_time DESC LIMIT 100", login_customer_id=login, max_rows=100)
    except Exception:  # the change history is a bonus; never block a write because of it
        rows = []
    for r in rows:
        paths = re.findall(r'paths: "([^"]+)"', str(r.get("change_event.changed_fields") or ""))
        if any(p.split(".")[0] in BIDDING_FIELDS for p in paths):
            dates.append(dt.datetime.fromisoformat(str(r["change_event.change_date_time"])[:19]))
    return max(dates).strftime("%Y-%m-%d") if dates else None



def check_values(strategy: str, target_cpa: float | None, target_roas_percent: float | None,
                 cpc_bid_ceiling: float | None, impression_share_location: str | None,
                 impression_share_percent: float | None, profile: Profile) -> None:
    """Only the values that belong to the strategy, within the profile's limits."""
    if target_cpa is not None and strategy not in ("MAXIMIZE_CONVERSIONS", "TARGET_CPA"):
        raise WriteRefused("target_cpa only goes with MAXIMIZE_CONVERSIONS.")
    if target_roas_percent is not None and strategy not in ("MAXIMIZE_CONVERSION_VALUE", "TARGET_ROAS"):
        raise WriteRefused("target_roas_percent only goes with MAXIMIZE_CONVERSION_VALUE.")
    if cpc_bid_ceiling is not None and strategy not in ("TARGET_SPEND", "TARGET_IMPRESSION_SHARE"):
        raise WriteRefused("cpc_bid_ceiling only goes with TARGET_SPEND or TARGET_IMPRESSION_SHARE.")
    if (impression_share_location or impression_share_percent is not None) and strategy != "TARGET_IMPRESSION_SHARE":
        raise WriteRefused("impression_share_location and impression_share_percent only go with TARGET_IMPRESSION_SHARE.")
    for name, value in (("target_cpa", target_cpa), ("target_roas_percent", target_roas_percent),
                        ("cpc_bid_ceiling", cpc_bid_ceiling)):
        if value is not None and value <= 0:
            raise WriteRefused(f"{name} must be greater than 0.")
    if target_cpa is not None and profile.max_target_cpa is not None and target_cpa > profile.max_target_cpa:
        raise WriteRefused(f"{target_cpa:.2f} is above this profile's max_target_cpa of {profile.max_target_cpa:.2f}.")
    if strategy in ("TARGET_SPEND", "TARGET_IMPRESSION_SHARE") and profile.max_cpc is not None:
        if cpc_bid_ceiling is None:
            raise WriteRefused(f"This profile sets max_cpc ({profile.max_cpc:.2f}), so cpc_bid_ceiling is required.")
        if cpc_bid_ceiling > profile.max_cpc:
            raise WriteRefused(f"cpc_bid_ceiling {cpc_bid_ceiling:.2f} is above this profile's max_cpc of "
                               f"{profile.max_cpc:.2f}.")
    if strategy == "TARGET_IMPRESSION_SHARE":
        if str(impression_share_location or "").upper() not in LOCATIONS:
            raise WriteRefused(f"impression_share_location must be one of: {', '.join(LOCATIONS)}.")
        if impression_share_percent is None or not 0 < impression_share_percent <= 100:
            raise WriteRefused("impression_share_percent must be between 1 and 100.")


def apply_standard(client, campaign, strategy: str, target_cpa: float | None, target_roas_percent: float | None,
                   cpc_bid_ceiling: float | None, impression_share_location: str | None,
                   impression_share_percent: float | None) -> tuple[list[str], dict[str, Any]]:
    """Set a standard strategy on a Campaign message (values checked before). Returns (mask paths, new state)."""
    new: dict[str, Any] = {"strategy": strategy}
    if strategy == "MAXIMIZE_CONVERSIONS":
        new["target_cpa"] = round(target_cpa, 2) if target_cpa else None
        campaign.maximize_conversions.target_cpa_micros = _micros(target_cpa) if target_cpa else 0
        return ["maximize_conversions.target_cpa_micros"], new
    if strategy == "MAXIMIZE_CONVERSION_VALUE":
        new["target_roas_percent"] = round(target_roas_percent, 1) if target_roas_percent else None
        campaign.maximize_conversion_value.target_roas = (target_roas_percent / 100) if target_roas_percent else 0
        return ["maximize_conversion_value.target_roas"], new
    if strategy == "TARGET_SPEND":
        new["cpc_bid_ceiling"] = round(cpc_bid_ceiling, 2) if cpc_bid_ceiling else None
        campaign.target_spend.cpc_bid_ceiling_micros = _micros(cpc_bid_ceiling) if cpc_bid_ceiling else 0
        return ["target_spend.cpc_bid_ceiling_micros"], new
    if strategy == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False
        return ["manual_cpc.enhanced_cpc_enabled"], new
    location = str(impression_share_location or "").upper()
    new.update(location=location, impression_share_percent=round(impression_share_percent, 1),
               cpc_bid_ceiling=round(cpc_bid_ceiling, 2) if cpc_bid_ceiling else None)
    campaign.target_impression_share.location = client.enums.TargetImpressionShareLocationEnum[location]
    campaign.target_impression_share.location_fraction_micros = int(round(impression_share_percent * 10_000))
    campaign.target_impression_share.cpc_bid_ceiling_micros = _micros(cpc_bid_ceiling) if cpc_bid_ceiling else 0
    return ["target_impression_share.location", "target_impression_share.location_fraction_micros",
            "target_impression_share.cpc_bid_ceiling_micros"], new


def plan_bidding_strategy(ads: Ads, cid: str, login: str | None, campaign_id: str, strategy: str,
                          target_cpa: float | None, target_roas_percent: float | None, cpc_bid_ceiling: float | None,
                          impression_share_location: str | None, impression_share_percent: float | None,
                          profile: Profile, logged: list[dict[str, Any]], is_revert: bool = False) -> Plan:
    strategy = str(strategy or "").strip().upper()
    if strategy not in STRATEGIES:
        raise WriteRefused(f"strategy must be one of: {', '.join(STRATEGIES)}.")
    if profile.allowed_bidding_strategies is not None and strategy not in profile.allowed_bidding_strategies:
        raise WriteRefused(f"This profile allows only these strategies: {', '.join(sorted(profile.allowed_bidding_strategies))} "
                           "(allowed_bidding_strategies).")
    if not re.fullmatch(r"\d+", str(campaign_id).strip()):
        raise WriteRefused("campaign_id must be a number.")

    rows, _ = ads.search(cid, "SELECT campaign.resource_name, campaign.name, campaign.status, "
                         "campaign.advertising_channel_type, campaign.bidding_strategy, campaign.bidding_strategy_type, "
                         f"{', '.join(READ_FIELDS)}, customer.currency_code FROM campaign WHERE campaign.id = {campaign_id}",
                         login_customer_id=login, max_rows=1)
    if not rows or rows[0]["campaign.status"] == "REMOVED":
        raise WriteRefused(f"Campaign {campaign_id} not found (or removed) in account {format_customer_id(cid)}.")
    row = rows[0]
    label = f"campaign '{row['campaign.name']}'"
    currency = row.get("customer.currency_code") or ""
    channel = row["campaign.advertising_channel_type"]
    if row.get("campaign.bidding_strategy"):
        raise WriteRefused(f"{label} uses a portfolio bid strategy. To take it out of the portfolio, use "
                           "manage_portfolio_bidding_strategy with action unassign.")
    if channel == "PERFORMANCE_MAX" and strategy not in PMAX_STRATEGIES:
        raise WriteRefused(f"{label} is Performance Max, which only supports {' and '.join(PMAX_STRATEGIES)}.")
    if channel not in ("SEARCH", "PERFORMANCE_MAX"):
        raise WriteRefused(f"{label} is a {channel} campaign. ads-pilot switches strategies only for Search and "
                           "Performance Max campaigns.")

    check_values(strategy, target_cpa, target_roas_percent, cpc_bid_ceiling, impression_share_location,
                 impression_share_percent, profile)
    client = ads.client(login)
    op = client.get_type("CampaignOperation")
    op.update.resource_name = row["campaign.resource_name"]
    paths, new = apply_standard(client, op.update, strategy, target_cpa, target_roas_percent, cpc_bid_ceiling,
                                impression_share_location, impression_share_percent)

    old = current_state(row)
    plan = Plan("CampaignService", "mutate_campaigns")
    if old == new:
        return plan
    target_key = {"MAXIMIZE_CONVERSIONS": "target_cpa", "MAXIMIZE_CONVERSION_VALUE": "target_roas_percent"}.get(strategy)
    if old["strategy"] == strategy and target_key and old.get(target_key) and new.get(target_key) and not is_revert:
        raise WriteRefused(f"{label} already uses {LABELS[strategy]} with a target. To change the target, use "
                           "set_bidding_target (it keeps the step limit).")
    client.copy_from(op.update_mask, _mask(*paths))
    plan.operations.append(op)
    plan.changes.append(Change(f"bid strategy of {label}: {describe(old, currency)} -> {describe(new, currency)}",
                               "bidding_strategy", old, new, row["campaign.resource_name"],
                               object_type="campaign_bidding_strategy", object_id=str(campaign_id),
                               name=row["campaign.name"]))

    # Warnings for the dry run.
    if old["strategy"] == strategy and target_key:
        plan.warnings.append("Adding or removing a target changes how smart bidding bids and starts a new learning "
                             "phase of about 1 to 2 weeks.")
    if old["strategy"] != strategy:
        plan.warnings.append("Every strategy switch starts a learning phase of about 1 to 2 weeks. Results swing in "
                             "that time; judge them afterwards.")
    if strategy in CONVERSION_STRATEGIES and old["strategy"] != strategy:
        stats, _ = ads.search(cid, "SELECT metrics.conversions FROM campaign WHERE campaign.id = "
                              f"{campaign_id} AND segments.date DURING LAST_30_DAYS", login_customer_id=login, max_rows=1)
        conversions = round(float(stats[0].get("metrics.conversions") or 0), 1) if stats else 0.0
        note = f"{label} had {conversions:g} primary conversions in the last 30 days."
        if conversions < MIN_CONVERSIONS:
            note += (f" That is under about {MIN_CONVERSIONS}: smart bidding has little data to learn from and may "
                     "spend unevenly at first.")
        plan.warnings.append(note)
    if strategy == "TARGET_SPEND" and not cpc_bid_ceiling:
        plan.warnings.append("Maximize clicks without cpc_bid_ceiling: Google may pay any price per click within the "
                             "budget.")
    if strategy == "MANUAL_CPC":
        plan.warnings.append("With manual CPC, the max CPC of ad groups and keywords decides the bids "
                             "(set_keyword_bid). Check them before confirming.")
    last = last_bidding_change(ads, cid, login, campaign_id, logged)
    if last:
        plan.warnings.append(f"The bidding of {label} was last changed on {last}, less than {LEARNING_DAYS} days ago: "
                             "the learning phase is probably still running.")
    return plan


def revert_arguments(old: dict[str, Any]) -> dict[str, Any]:
    """Arguments for plan_bidding_strategy that restore a logged old state."""
    strategy = old.get("strategy")
    if strategy == "TARGET_CPA":  # Google now offers Target CPA as Maximize conversions with a target
        strategy = "MAXIMIZE_CONVERSIONS"
    if strategy == "TARGET_ROAS":
        strategy = "MAXIMIZE_CONVERSION_VALUE"
    if strategy not in STRATEGIES:
        raise WriteRefused(f"The old strategy {old.get('strategy')} cannot be set with ads-pilot. Change it in Google Ads.")
    return {"strategy": strategy, "target_cpa": old.get("target_cpa"),
            "target_roas_percent": old.get("target_roas_percent"), "cpc_bid_ceiling": old.get("cpc_bid_ceiling"),
            "impression_share_location": old.get("location"),
            "impression_share_percent": old.get("impression_share_percent")}
