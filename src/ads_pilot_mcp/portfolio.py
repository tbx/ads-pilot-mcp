# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Portfolio bid strategies: one strategy shared by several campaigns, each keeping its own budget.

list_bidding_strategies reads them. manage_portfolio_bidding_strategy (access level full) can:
  create    a portfolio and assign campaigns to it, in one atomic request
  assign    campaigns to an existing portfolio
  unassign  campaigns back to a standard strategy (given as parameter)
  update    the targets of a portfolio (same limits as set_bidding_target)
  remove    a portfolio that no campaign uses any more
Shared budgets are never created or changed here. Portfolios that Google "aligned" with a shared
budget (created together with it in the Google Ads UI) only work with that budget: ads-pilot does
not assign campaigns to them and does not remove them (Google requires removing both together). Campaign states are logged as dicts, so
revert_change can restore the old strategy or portfolio with its values.
"""

from __future__ import annotations

import re
from typing import Any

from . import strategy
from .ads import Ads
from .config import Profile, format_customer_id
from .writes import Change, Plan, WriteRefused, _mask, _quote

ACTIONS = ("create", "assign", "unassign", "update", "remove")
TYPES = ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "TARGET_CPA", "TARGET_ROAS", "TARGET_SPEND")
# type -> (field group in the BiddingStrategy message, which values it takes)
GROUPS = {"MAXIMIZE_CONVERSIONS": "maximize_conversions", "MAXIMIZE_CONVERSION_VALUE": "maximize_conversion_value",
          "TARGET_CPA": "target_cpa", "TARGET_ROAS": "target_roas", "TARGET_SPEND": "target_spend"}
CPA_FIELD = {"MAXIMIZE_CONVERSIONS": "target_cpa_micros", "TARGET_CPA": "target_cpa_micros"}
ROAS_FIELD = {"MAXIMIZE_CONVERSION_VALUE": "target_roas", "TARGET_ROAS": "target_roas"}
MAX_CAMPAIGNS = 50

STRATEGY_FIELDS = ("bidding_strategy.resource_name", "bidding_strategy.id", "bidding_strategy.name",
                   "bidding_strategy.type", "bidding_strategy.status", "bidding_strategy.non_removed_campaign_count",
                   "bidding_strategy.aligned_campaign_budget_id",
                   "bidding_strategy.maximize_conversions.target_cpa_micros",
                   "bidding_strategy.maximize_conversions.cpc_bid_ceiling_micros",
                   "bidding_strategy.maximize_conversion_value.target_roas",
                   "bidding_strategy.maximize_conversion_value.cpc_bid_ceiling_micros",
                   "bidding_strategy.target_cpa.target_cpa_micros", "bidding_strategy.target_cpa.cpc_bid_ceiling_micros",
                   "bidding_strategy.target_roas.target_roas", "bidding_strategy.target_roas.cpc_bid_ceiling_micros",
                   "bidding_strategy.target_spend.cpc_bid_ceiling_micros")
CAMPAIGN_FIELDS = ("campaign.resource_name", "campaign.id", "campaign.name", "campaign.status",
                   "campaign.advertising_channel_type", "campaign.bidding_strategy", "campaign.bidding_strategy_type",
                   "campaign.bidding_strategy_system_status", "campaign.campaign_budget",
                   "campaign_budget.amount_micros", "campaign_budget.explicitly_shared", *strategy.READ_FIELDS,
                   "customer.currency_code")


def _money(micros: int | None) -> float | None:
    return round(micros / 1_000_000, 2) if micros else None


def portfolio_state(row: dict[str, Any]) -> dict[str, Any]:
    """Name, ID, type and values of a portfolio row, as logged and shown."""
    kind = row["bidding_strategy.type"]
    group = GROUPS.get(kind, "")
    state: dict[str, Any] = {"portfolio": row["bidding_strategy.name"], "portfolio_id": str(row["bidding_strategy.id"]),
                             "strategy": kind}
    if kind in CPA_FIELD:
        state["target_cpa"] = _money(row.get(f"bidding_strategy.{group}.target_cpa_micros"))
    if kind in ROAS_FIELD:
        roas = row.get(f"bidding_strategy.{group}.target_roas")
        state["target_roas_percent"] = round(roas * 100, 1) if roas else None
    state["cpc_bid_ceiling"] = _money(row.get(f"bidding_strategy.{group}.cpc_bid_ceiling_micros"))
    return state


def describe(state: dict[str, Any], currency: str) -> str:
    if "portfolio" not in state:
        return strategy.describe(state, currency)
    text = f"portfolio '{state['portfolio']}': " + strategy.describe(
        {k: v for k, v in state.items() if k not in ("portfolio", "portfolio_id")}, currency)
    return text.replace(", no max. CPC", "")


def _portfolios(ads: Ads, cid: str, login: str | None, where: str = "") -> list[dict[str, Any]]:
    rows, _ = ads.search(cid, f"SELECT {', '.join(STRATEGY_FIELDS)} FROM bidding_strategy {where}",
                         login_customer_id=login, max_rows=200)
    return rows


def _portfolio(ads: Ads, cid: str, login: str | None, portfolio_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d+", str(portfolio_id or "").strip()):
        raise WriteRefused("portfolio_id must be a number (see list_bidding_strategies).")
    rows = _portfolios(ads, cid, login, f"WHERE bidding_strategy.id = {portfolio_id}")
    if not rows or rows[0]["bidding_strategy.status"] == "REMOVED":
        raise WriteRefused(f"Portfolio bid strategy {portfolio_id} not found (or removed) in account "
                           f"{format_customer_id(cid)}.")
    return rows[0]


def _campaigns(ads: Ads, cid: str, login: str | None, where: str) -> list[dict[str, Any]]:
    rows, _ = ads.search(cid, f"SELECT {', '.join(CAMPAIGN_FIELDS)} FROM campaign {where}",
                         login_customer_id=login, max_rows=500)
    return rows


def _load_campaigns(ads: Ads, cid: str, login: str | None, ids: list[str]) -> list[dict[str, Any]]:
    ids = [str(i).strip() for i in ids or []]
    if not ids or len(ids) > MAX_CAMPAIGNS:
        raise WriteRefused(f"Give between 1 and {MAX_CAMPAIGNS} campaign IDs.")
    bad = [i for i in ids if not re.fullmatch(r"\d+", i)]
    if bad:
        raise WriteRefused(f"Invalid campaign ID(s) {_quote(bad)}.")
    ids = list(dict.fromkeys(ids))
    rows = {str(r["campaign.id"]): r for r in _campaigns(ads, cid, login, f"WHERE campaign.id IN ({', '.join(ids)})")}
    missing = [i for i in ids if i not in rows or rows[i]["campaign.status"] == "REMOVED"]
    if missing:
        raise WriteRefused(f"Campaign(s) not found (or removed) in account {format_customer_id(cid)}: {_quote(missing)}.")
    for r in rows.values():
        if r["campaign.advertising_channel_type"] != "SEARCH":
            raise WriteRefused(f"Campaign '{r['campaign.name']}' is {r['campaign.advertising_channel_type']}. ads-pilot "
                               "puts only Search campaigns into portfolio strategies (Performance Max does not "
                               "support them).")
    return [rows[i] for i in ids]


def _not_aligned(row: dict[str, Any], what: str) -> None:
    budget = row.get("bidding_strategy.aligned_campaign_budget_id")
    if budget:
        raise WriteRefused(f"Portfolio '{row['bidding_strategy.name']}' is tied to shared budget {budget} (created together "
                           f"in Google Ads). ads-pilot does not touch shared budgets, so it does not {what}. Create a new "
                           "portfolio with action create instead, or do it in Google Ads.")


def campaign_state(ads: Ads, cid: str, login: str | None, row: dict[str, Any],
                   cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rn = row.get("campaign.bidding_strategy")
    if not rn:
        return strategy.current_state(row)
    if rn not in cache:
        found = _portfolios(ads, cid, login, f"WHERE bidding_strategy.resource_name = '{rn}'")
        cache[rn] = portfolio_state(found[0]) if found else {"portfolio": rn, "portfolio_id": rn.rsplit("/", 1)[-1],
                                                              "strategy": row["campaign.bidding_strategy_type"]}
    return cache[rn]


# ---- reading -------------------------------------------------------------------------------------------------------

def list_bidding_strategies(ads: Ads, cid: str, login: str | None) -> dict[str, Any]:
    portfolios = _portfolios(ads, cid, login, "WHERE bidding_strategy.status != 'REMOVED'")
    campaigns = _campaigns(ads, cid, login, "WHERE campaign.status != 'REMOVED' AND campaign.bidding_strategy IS NOT NULL")
    budgets = _shared_budget_users(ads, cid, login)
    currency = ads.currency(cid, login)
    out = []
    for p in portfolios:
        members = [c for c in campaigns if c["campaign.bidding_strategy"] == p["bidding_strategy.resource_name"]]
        state = portfolio_state(p)
        aligned = p.get("bidding_strategy.aligned_campaign_budget_id")
        out.append({**state, "description": describe(state, currency),
                    "campaign_count": p.get("bidding_strategy.non_removed_campaign_count"),
                    "aligned_shared_budget_id": str(aligned) if aligned else None,
                    "campaigns": [{"id": str(c["campaign.id"]), "name": c["campaign.name"], "status": c["campaign.status"],
                                   "system_status": c.get("campaign.bidding_strategy_system_status"),
                                   "daily_budget": _money(c.get("campaign_budget.amount_micros")),
                                   "shared_budget": bool(c.get("campaign_budget.explicitly_shared")),
                                   "shares_budget_outside_portfolio": _outside(c, members, budgets)} for c in members]})
    return {"portfolio_bidding_strategies": out, "count": len(out),
            "note": "system_status shows Google's view, e.g. LEARNING_NEW or MISCONFIGURED_SHARED_BUDGET. Portfolios with "
                    "an aligned_shared_budget_id are tied to that shared budget; ads-pilot does not assign campaigns to "
                    "them or remove them."}


def _shared_budget_users(ads: Ads, cid: str, login: str | None) -> dict[str, list[str]]:
    rows, _ = ads.search(cid, "SELECT campaign.id, campaign.campaign_budget FROM campaign WHERE campaign.status != 'REMOVED' "
                         "AND campaign_budget.explicitly_shared = TRUE", login_customer_id=login, max_rows=500)
    users: dict[str, list[str]] = {}
    for r in rows:
        users.setdefault(r["campaign.campaign_budget"], []).append(str(r["campaign.id"]))
    return users


def _outside(row: dict[str, Any], members: list[dict[str, Any]], budgets: dict[str, list[str]]) -> bool:
    if not row.get("campaign_budget.explicitly_shared"):
        return False
    member_ids = {str(m["campaign.id"]) for m in members}
    return any(i not in member_ids for i in budgets.get(row["campaign.campaign_budget"], []))


# ---- checks shown in the dry run -------------------------------------------------------------------------------

def _warnings(ads: Ads, cid: str, login: str | None, rows: list[dict[str, Any]], members_after: list[str],
              logged: list[dict[str, Any]], plan: Plan) -> None:
    plan.warnings.append("Every strategy switch starts a learning phase of about 1 to 2 weeks. Results swing in that "
                         "time; judge them afterwards.")
    ids = ", ".join(members_after)
    stats, _ = ads.search(cid, f"SELECT campaign.id, campaign.name, metrics.conversions FROM campaign WHERE campaign.id IN "
                          f"({ids}) AND segments.date DURING LAST_30_DAYS", login_customer_id=login, max_rows=500)
    per: dict[str, list] = {}
    for s in stats:
        entry = per.setdefault(str(s["campaign.id"]), [s["campaign.name"], 0.0])
        entry[1] += float(s.get("metrics.conversions") or 0)
    total = sum(n for _, n in per.values())
    listed = ", ".join(f"'{name}' {n:g}" for name, n in per.values()) or "none"
    note = f"Primary conversions in the last 30 days: {total:g} in the portfolio ({listed})."
    if total < strategy.MIN_CONVERSIONS:
        note += (f" That is under about {strategy.MIN_CONVERSIONS} in total: smart bidding has little data to learn "
                 "from, even together.")
    plan.warnings.append(note)
    for r in rows:
        last = strategy.last_bidding_change(ads, cid, login, str(r["campaign.id"]), logged)
        if last:
            plan.warnings.append(f"The bidding of campaign '{r['campaign.name']}' was last changed on {last}, less than "
                                 f"{strategy.LEARNING_DAYS} days ago: its learning phase is probably still running.")
    # Do all campaigns bid on the same goals? And which actions count for them?
    goals, _ = ads.search(cid, "SELECT campaign.id, campaign.name, campaign_conversion_goal.category, "
                          "campaign_conversion_goal.origin FROM campaign_conversion_goal WHERE campaign_conversion_goal.biddable "
                          f"= TRUE AND campaign.id IN ({ids})", login_customer_id=login, max_rows=1000)
    sets: dict[str, set] = {}
    for g in goals:
        sets.setdefault(str(g["campaign.id"]), set()).add((g["campaign_conversion_goal.category"],
                                                           g["campaign_conversion_goal.origin"]))
    if len({frozenset(s) for s in sets.values()}) > 1:
        names = {str(g["campaign.id"]): g["campaign.name"] for g in goals}
        detail = "; ".join(f"'{names[c]}': {', '.join(sorted(f'{a} ({b})' for a, b in s))}" for c, s in sets.items())
        plan.warnings.append(f"The campaigns bid on different conversion goals: {detail}. A portfolio learns from all of "
                             "them together.")
    used = set().union(*sets.values()) if sets else set()
    if used:
        actions, _ = ads.search(cid, "SELECT conversion_action.name, conversion_action.category, conversion_action.origin "
                                "FROM conversion_action WHERE conversion_action.status = 'ENABLED' AND "
                                "conversion_action.primary_for_goal = TRUE", login_customer_id=login, max_rows=500)
        for category, origin in sorted(used):
            names = [a["conversion_action.name"] for a in actions
                     if (a["conversion_action.category"], a["conversion_action.origin"]) == (category, origin)]
            if len(names) > 1:
                plan.warnings.append(f"Goal {category} ({origin}) counts {len(names)} primary actions for every campaign: "
                                     f"{', '.join(repr(n) for n in names)}. If they belong to different websites, the "
                                     "campaigns optimize for all of them.")
    budgets = _shared_budget_users(ads, cid, login)
    member_rows = [{"campaign.id": i} for i in members_after]
    for r in rows:
        if _outside(r, member_rows, budgets):
            plan.warnings.append(f"Campaign '{r['campaign.name']}' shares its budget with campaigns outside this "
                                 "portfolio. Google then shows the strategy as misconfigured.")


def _values(kind: str, target_cpa: float | None, target_roas_percent: float | None, cpc_bid_ceiling: float | None,
            profile: Profile) -> None:
    if kind not in TYPES:
        raise WriteRefused(f"strategy must be one of: {', '.join(TYPES)}.")
    if profile.allowed_bidding_strategies is not None and kind not in profile.allowed_bidding_strategies:
        raise WriteRefused(f"This profile allows only these strategies: {', '.join(sorted(profile.allowed_bidding_strategies))} "
                           "(allowed_bidding_strategies).")
    if target_cpa is not None and kind not in CPA_FIELD:
        raise WriteRefused("target_cpa only goes with MAXIMIZE_CONVERSIONS or TARGET_CPA.")
    if target_roas_percent is not None and kind not in ROAS_FIELD:
        raise WriteRefused("target_roas_percent only goes with MAXIMIZE_CONVERSION_VALUE or TARGET_ROAS.")
    if kind == "TARGET_CPA" and not target_cpa:
        raise WriteRefused("TARGET_CPA needs target_cpa.")
    if kind == "TARGET_ROAS" and not target_roas_percent:
        raise WriteRefused("TARGET_ROAS needs target_roas_percent.")
    for name, value in (("target_cpa", target_cpa), ("target_roas_percent", target_roas_percent),
                        ("cpc_bid_ceiling", cpc_bid_ceiling)):
        if value is not None and value <= 0:
            raise WriteRefused(f"{name} must be greater than 0.")
    if target_cpa is not None and profile.max_target_cpa is not None and target_cpa > profile.max_target_cpa:
        raise WriteRefused(f"{target_cpa:.2f} is above this profile's max_target_cpa of {profile.max_target_cpa:.2f}.")
    if profile.max_cpc is not None:
        if kind == "TARGET_SPEND" and cpc_bid_ceiling is None:
            raise WriteRefused(f"This profile sets max_cpc ({profile.max_cpc:.2f}), so cpc_bid_ceiling is required.")
        if cpc_bid_ceiling is not None and cpc_bid_ceiling > profile.max_cpc:
            raise WriteRefused(f"cpc_bid_ceiling {cpc_bid_ceiling:.2f} is above this profile's max_cpc of {profile.max_cpc:.2f}.")


def _set_values(message, kind: str, target_cpa: float | None, target_roas_percent: float | None,
                cpc_bid_ceiling: float | None) -> list[str]:
    group = GROUPS[kind]
    sub = getattr(message, group)
    paths = []
    if kind in CPA_FIELD:
        sub.target_cpa_micros = strategy._micros(target_cpa) if target_cpa else 0
        paths.append(f"{group}.target_cpa_micros")
    if kind in ROAS_FIELD:
        sub.target_roas = (target_roas_percent / 100) if target_roas_percent else 0
        paths.append(f"{group}.target_roas")
    if cpc_bid_ceiling:  # Google rejects 0 as a ceiling for some types, so only send a real value
        sub.cpc_bid_ceiling_micros = strategy._micros(cpc_bid_ceiling)
        paths.append(f"{group}.cpc_bid_ceiling_micros")
    return paths


def _state(name: str, pid: str | None, kind: str, target_cpa, target_roas_percent, cpc_bid_ceiling) -> dict[str, Any]:
    state: dict[str, Any] = {"portfolio": name, "portfolio_id": pid, "strategy": kind}
    if kind in CPA_FIELD:
        state["target_cpa"] = round(target_cpa, 2) if target_cpa else None
    if kind in ROAS_FIELD:
        state["target_roas_percent"] = round(target_roas_percent, 1) if target_roas_percent else None
    state["cpc_bid_ceiling"] = round(cpc_bid_ceiling, 2) if cpc_bid_ceiling else None
    return state


def _campaign_change(row: dict[str, Any], old: dict[str, Any], new: dict[str, Any]) -> Change:
    currency = row.get("customer.currency_code") or ""
    budget = _money(row.get("campaign_budget.amount_micros"))
    shared = " (shared budget)" if row.get("campaign_budget.explicitly_shared") else ""
    return Change(f"bid strategy of campaign '{row['campaign.name']}': {describe(old, currency)} -> "
                  f"{describe(new, currency)}; budget stays {budget or 0:.2f} {currency}/day{shared}",
                  "bidding_strategy", old, new, row["campaign.resource_name"], object_type="campaign_bidding_strategy",
                  object_id=str(row["campaign.id"]), name=row["campaign.name"])


# ---- the write tool ----------------------------------------------------------------------------------------------

def plan_portfolio(ads: Ads, cid: str, login: str | None, action: str, profile: Profile, logged: list[dict[str, Any]],
                   name: str | None = None, portfolio_id: str | None = None, strategy_type: str | None = None,
                   target_cpa: float | None = None, target_roas_percent: float | None = None,
                   cpc_bid_ceiling: float | None = None, campaign_ids: list[str] | None = None,
                   standard_strategy: str | None = None, impression_share_location: str | None = None,
                   impression_share_percent: float | None = None, is_revert: bool = False) -> Plan:
    if action not in ACTIONS:
        raise WriteRefused(f"action must be one of: {', '.join(ACTIONS)}.")
    client = ads.client(login)
    cache: dict[str, dict[str, Any]] = {}

    if action == "create":
        name = str(name or "").strip()
        if not name or len(name) > 255:
            raise WriteRefused("create needs a name (max 255 characters).")
        kind = str(strategy_type or "").upper()
        _values(kind, target_cpa, target_roas_percent, cpc_bid_ceiling, profile)
        taken = [p for p in _portfolios(ads, cid, login, "WHERE bidding_strategy.status != 'REMOVED'")
                 if p["bidding_strategy.name"].strip().lower() == name.lower()]
        if taken:
            raise WriteRefused(f"A portfolio named '{name}' exists already (ID {taken[0]['bidding_strategy.id']}). "
                               "Use assign, or choose another name.")
        rows = _load_campaigns(ads, cid, login, campaign_ids)
        temp = f"customers/{cid}/biddingStrategies/-1"
        plan = Plan("GoogleAdsService", "mutate")
        op = client.get_type("MutateOperation")
        bs = op.bidding_strategy_operation.create
        bs.resource_name = temp
        bs.name = name
        _set_values(bs, kind, target_cpa, target_roas_percent, cpc_bid_ceiling)
        plan.operations.append(op)
        new = _state(name, None, kind, target_cpa, target_roas_percent, cpc_bid_ceiling)
        currency = rows[0].get("customer.currency_code") or ""
        plan.changes.append(Change(f"new {describe(new, currency)}", "portfolio", None, new, temp,
                                   object_type="portfolio_bidding_strategy", name=name))
        for r in rows:
            op = client.get_type("MutateOperation")
            op.campaign_operation.update.resource_name = r["campaign.resource_name"]
            op.campaign_operation.update.bidding_strategy = temp
            client.copy_from(op.campaign_operation.update_mask, _mask("bidding_strategy"))
            plan.operations.append(op)
            plan.changes.append(_campaign_change(r, campaign_state(ads, cid, login, r, cache), new))
        plan.warnings.append("Budgets are not touched: every campaign keeps its own budget.")
        _warnings(ads, cid, login, rows, [str(r["campaign.id"]) for r in rows], logged, plan)
        return plan

    if action == "assign":
        p = _portfolio(ads, cid, login, portfolio_id)
        _not_aligned(p, "assign campaigns to it")
        target = portfolio_state(p)
        rows = _load_campaigns(ads, cid, login, campaign_ids)
        plan = Plan("CampaignService", "mutate_campaigns", partial_failure=True)
        todo = []
        for r in rows:
            if r.get("campaign.bidding_strategy") == p["bidding_strategy.resource_name"]:
                plan.warnings.append(f"Campaign '{r['campaign.name']}' already uses this portfolio, skipped.")
                continue
            op = client.get_type("CampaignOperation")
            op.update.resource_name = r["campaign.resource_name"]
            op.update.bidding_strategy = p["bidding_strategy.resource_name"]
            client.copy_from(op.update_mask, _mask("bidding_strategy"))
            plan.operations.append(op)
            plan.changes.append(_campaign_change(r, campaign_state(ads, cid, login, r, cache), target))
            todo.append(r)
        if todo and not is_revert:
            members = _campaigns(ads, cid, login, f"WHERE campaign.bidding_strategy = '{p['bidding_strategy.resource_name']}' "
                                 "AND campaign.status != 'REMOVED'")
            after = list(dict.fromkeys([str(m["campaign.id"]) for m in members] + [str(r["campaign.id"]) for r in todo]))
            plan.warnings.append("Budgets are not touched: every campaign keeps its own budget.")
            _warnings(ads, cid, login, todo, after, logged, plan)
        return plan

    if action == "unassign":
        kind = str(standard_strategy or "").upper()
        if kind not in strategy.STRATEGIES:
            raise WriteRefused(f"unassign needs standard_strategy, one of: {', '.join(strategy.STRATEGIES)}.")
        if profile.allowed_bidding_strategies is not None and kind not in profile.allowed_bidding_strategies:
            raise WriteRefused("This profile allows only these strategies: "
                               f"{', '.join(sorted(profile.allowed_bidding_strategies))} (allowed_bidding_strategies).")
        strategy.check_values(kind, target_cpa, target_roas_percent, cpc_bid_ceiling, impression_share_location,
                              impression_share_percent, profile)
        rows = _load_campaigns(ads, cid, login, campaign_ids)
        plan = Plan("CampaignService", "mutate_campaigns", partial_failure=True)
        for r in rows:
            if not r.get("campaign.bidding_strategy"):
                raise WriteRefused(f"Campaign '{r['campaign.name']}' does not use a portfolio. Use set_bidding_strategy.")
            if portfolio_id and r["campaign.bidding_strategy"].rsplit("/", 1)[-1] != str(portfolio_id):
                raise WriteRefused(f"Campaign '{r['campaign.name']}' is not in portfolio {portfolio_id}.")
            op = client.get_type("CampaignOperation")
            op.update.resource_name = r["campaign.resource_name"]
            paths, new = strategy.apply_standard(client, op.update, kind, target_cpa, target_roas_percent,
                                                 cpc_bid_ceiling, impression_share_location, impression_share_percent)
            client.copy_from(op.update_mask, _mask(*paths))
            plan.operations.append(op)
            plan.changes.append(_campaign_change(r, campaign_state(ads, cid, login, r, cache), new))
        if not is_revert:
            plan.warnings.append("Leaving a portfolio starts a new learning phase of about 1 to 2 weeks for each campaign.")
        return plan

    p = _portfolio(ads, cid, login, portfolio_id)
    old = portfolio_state(p)
    label = f"portfolio bid strategy '{p['bidding_strategy.name']}'"

    if action == "remove":
        _not_aligned(p, "remove it (Google removes such a portfolio only together with its budget)")
        count = p.get("bidding_strategy.non_removed_campaign_count") or 0
        if count:
            raise WriteRefused(f"{label} is still used by {count} campaign(s). Unassign them first.")
        plan = Plan("BiddingStrategyService", "mutate_bidding_strategies")
        op = client.get_type("BiddingStrategyOperation")
        op.remove = p["bidding_strategy.resource_name"]
        plan.operations.append(op)
        plan.changes.append(Change(label, "status", "ENABLED", "REMOVED", p["bidding_strategy.resource_name"],
                                   object_type="portfolio_bidding_strategy", object_id=old["portfolio_id"],
                                   name=old["portfolio"]))
        plan.warnings.append("Removing a portfolio is final; revert_change cannot bring it back.")
        return plan

    # update: change targets of the portfolio
    kind = old["strategy"]
    if kind not in TYPES:
        raise WriteRefused(f"{label} is {kind}; ads-pilot changes targets only of {', '.join(TYPES)}.")
    new_cpa = target_cpa if target_cpa is not None else old.get("target_cpa")
    new_roas = target_roas_percent if target_roas_percent is not None else old.get("target_roas_percent")
    new_ceiling = cpc_bid_ceiling if cpc_bid_ceiling is not None else old.get("cpc_bid_ceiling")
    _values(kind, target_cpa if kind in CPA_FIELD else None, target_roas_percent if kind in ROAS_FIELD else None,
            cpc_bid_ceiling, profile)
    if target_cpa is not None and kind not in CPA_FIELD or target_roas_percent is not None and kind not in ROAS_FIELD:
        raise WriteRefused(f"{label} is {kind}; that target does not apply.")
    new = _state(old["portfolio"], old["portfolio_id"], kind, new_cpa, new_roas, new_ceiling)
    plan = Plan("BiddingStrategyService", "mutate_bidding_strategies")
    if new == old:
        return plan
    if not is_revert:
        for key in ("target_cpa", "target_roas_percent"):
            if old.get(key) and new.get(key) and old[key] != new[key]:
                change = (new[key] - old[key]) / old[key] * 100
                if abs(change) > profile.max_target_change_percent:
                    raise WriteRefused(f"From {old[key]:g} to {new[key]:g} is a change of {change:+.0f}%. This profile "
                                       f"allows at most {profile.max_target_change_percent:.0f}% per step "
                                       "(max_target_change_percent). Change it in smaller steps.")
    op = client.get_type("BiddingStrategyOperation")
    op.update.resource_name = p["bidding_strategy.resource_name"]
    paths = _set_values(op.update, kind, new_cpa, new_roas, new_ceiling)
    client.copy_from(op.update_mask, _mask(*paths))
    plan.operations.append(op)
    rows = _campaigns(ads, cid, login, f"WHERE campaign.bidding_strategy = '{p['bidding_strategy.resource_name']}' "
                      "AND campaign.status != 'REMOVED'")
    currency = ads.currency(cid, login)
    plan.changes.append(Change(f"targets of {label} ({len(rows)} campaigns): {describe(old, currency)} -> "
                               f"{describe(new, currency)}", "targets", old, new, p["bidding_strategy.resource_name"],
                               object_type="portfolio_bidding_strategy", object_id=old["portfolio_id"],
                               name=old["portfolio"]))
    if rows:
        plan.warnings.append(f"This changes the bidding of all {len(rows)} campaigns in the portfolio: "
                             f"{', '.join(repr(c['campaign.name']) for c in rows)}. Smart bidding needs some days to "
                             "adjust; judge results after 1-2 weeks.")
    else:
        plan.warnings.append("No campaign uses this portfolio right now.")
    return plan


def plan_revert(ads: Ads, cid: str, login: str | None, entry: dict[str, Any], profile: Profile) -> Plan:
    old, new = entry.get("old"), entry.get("new")
    if entry.get("object_type") == "portfolio_bidding_strategy":
        if entry.get("field") != "targets":
            raise WriteRefused("Creating or removing a portfolio cannot be reverted. A created portfolio can be removed "
                               "with action remove once no campaign uses it.")
        plan = plan_portfolio(ads, cid, login, "update", profile, [], portfolio_id=entry.get("object_id"),
                              target_cpa=old.get("target_cpa"), target_roas_percent=old.get("target_roas_percent"),
                              cpc_bid_ceiling=old.get("cpc_bid_ceiling"), is_revert=True)
    else:
        rows = _load_campaigns(ads, cid, login, [entry.get("object_id")])
        current = campaign_state(ads, cid, login, rows[0], {})
        same = (current.get("portfolio") == new.get("portfolio") and current.get("strategy") == new.get("strategy")
                if "portfolio" in new else current == new)
        if not same:
            raise WriteRefused(f"Not reverted: the strategy changed since. Logged new state: {new}. Current state: "
                               f"{current}. Check in Google Ads and change it by hand if needed.")
        if "portfolio" in old:
            plan = plan_portfolio(ads, cid, login, "assign", profile, [], portfolio_id=old["portfolio_id"],
                                  campaign_ids=[entry.get("object_id")], is_revert=True)
        else:
            args = strategy.revert_arguments(old)
            plan = plan_portfolio(ads, cid, login, "unassign", profile, [], campaign_ids=[entry.get("object_id")],
                                  standard_strategy=args["strategy"], target_cpa=args["target_cpa"],
                                  target_roas_percent=args["target_roas_percent"], cpc_bid_ceiling=args["cpc_bid_ceiling"],
                                  impression_share_location=args["impression_share_location"],
                                  impression_share_percent=args["impression_share_percent"], is_revert=True)
    if not plan.changes:
        raise WriteRefused("Nothing to revert: the old state is already in place.")
    return plan
