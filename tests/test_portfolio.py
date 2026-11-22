"""Offline tests for portfolio bid strategies (list_bidding_strategies, manage_portfolio_bidding_strategy)."""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from ads_pilot_mcp.ads import MutateResult
from ads_pilot_mcp.config import Profile
from ads_pilot_mcp.server import create_server

DEMO = "1234567890"
PF = f"customers/{DEMO}/biddingStrategies/900"


def campaign(id_, name, channel="SEARCH", portfolio=None, kind="TARGET_SPEND", ceiling=1_000_000, budget=1,
             shared=False):
    return {"campaign.resource_name": f"customers/{DEMO}/campaigns/{id_}", "campaign.id": id_, "campaign.name": name,
            "campaign.status": "ENABLED", "campaign.advertising_channel_type": channel,
            "campaign.bidding_strategy": portfolio, "campaign.bidding_strategy_type": kind,
            "campaign.bidding_strategy_system_status": "ENABLED",
            "campaign.campaign_budget": f"customers/{DEMO}/campaignBudgets/{budget}",
            "campaign_budget.amount_micros": 10_000_000, "campaign_budget.explicitly_shared": shared,
            "campaign.target_spend.cpc_bid_ceiling_micros": ceiling, "campaign.maximize_conversions.target_cpa_micros": 0,
            "campaign.maximize_conversion_value.target_roas": 0.0, "campaign.target_cpa.target_cpa_micros": 0,
            "campaign.target_roas.target_roas": 0.0, "campaign.target_impression_share.location": "UNSPECIFIED",
            "campaign.target_impression_share.location_fraction_micros": 0,
            "campaign.target_impression_share.cpc_bid_ceiling_micros": 0, "customer.currency_code": "EUR"}


class FakeAccount:
    def __init__(self):
        self._client = GoogleAdsClient(credentials=AnonymousCredentials(), version="v26", use_proto_plus=True)
        self.mutations = []
        self.campaigns = {1: campaign(1, "Coffee DE"), 2: campaign(2, "Coffee EN", budget=2),
                          3: campaign(3, "Coffee PMax", channel="PERFORMANCE_MAX", kind="MAXIMIZE_CONVERSIONS")}
        self.portfolio = {"bidding_strategy.resource_name": PF, "bidding_strategy.id": 900,
                          "bidding_strategy.name": "Coffee shared", "bidding_strategy.type": "MAXIMIZE_CONVERSIONS",
                          "bidding_strategy.status": "ENABLED", "bidding_strategy.non_removed_campaign_count": 0,
                          "bidding_strategy.aligned_campaign_budget_id": 0,
                          "bidding_strategy.maximize_conversions.target_cpa_micros": 20_000_000,
                          "bidding_strategy.maximize_conversions.cpc_bid_ceiling_micros": 0}
        self.goals = {1: [("SUBMIT_LEAD_FORM", "WEBSITE")], 2: [("SUBMIT_LEAD_FORM", "WEBSITE")]}
        self.shared_users = {}

    def client(self, login_customer_id=None):
        return self._client

    def currency(self, customer_id, login_customer_id=None):
        return "EUR"

    def search(self, customer_id, query, login_customer_id=None, max_rows=200):
        if "FROM bidding_strategy" in query:
            if "bidding_strategy.id = 901" in query:
                return [], False
            return [self.portfolio], False
        if "campaign_budget.explicitly_shared = TRUE" in query:
            return [{"campaign.id": i, "campaign.campaign_budget": b} for i, b in self.shared_users.items()], False
        if "metrics.conversions FROM campaign" in query:
            return [{"campaign.id": 1, "campaign.name": "Coffee DE", "metrics.conversions": 6.0},
                    {"campaign.id": 2, "campaign.name": "Coffee EN", "metrics.conversions": 2.0}], False
        if "FROM campaign_conversion_goal" in query:
            return [{"campaign.id": c, "campaign.name": self.campaigns[c]["campaign.name"],
                     "campaign_conversion_goal.category": a, "campaign_conversion_goal.origin": b}
                    for c, goals in self.goals.items() for a, b in goals], False
        if "FROM conversion_action" in query:
            return [{"conversion_action.name": "Lead form", "conversion_action.category": "SUBMIT_LEAD_FORM",
                     "conversion_action.origin": "WEBSITE"}], False
        if "FROM change_event" in query:
            return [], False
        if "FROM campaign WHERE campaign.id IN" in query:
            ids = [int(i) for i in query.split("IN (")[1].split(")")[0].split(", ")]
            return [self.campaigns[i] for i in ids if i in self.campaigns], False
        if "FROM campaign WHERE campaign.bidding_strategy =" in query:
            return [c for c in self.campaigns.values() if c["campaign.bidding_strategy"] == PF], False
        if "FROM campaign WHERE" in query:
            return list(self.campaigns.values()), False
        raise AssertionError(f"unexpected query: {query}")

    def mutate(self, customer_id, service, method, operations, login_customer_id=None, validate_only=True,
               partial_failure=False):
        self.mutations.append((validate_only, service, len(operations)))
        return MutateResult(results=[f"r{i}" for i in range(len(operations))])

    def join(self, *ids):
        """Make campaigns members of the portfolio, as Google would after an assign."""
        for i in ids:
            self.campaigns[i].update({"campaign.bidding_strategy": PF, "campaign.bidding_strategy_type": "MAXIMIZE_CONVERSIONS"})
        self.portfolio["bidding_strategy.non_removed_campaign_count"] = len(ids)


@pytest.fixture
def account():
    return FakeAccount()


def server(account, tmp_path, level="full", **limits):
    profile = Profile("fjord", {"client_id": "x"}, default_customer_id=DEMO, account_levels={DEMO: level}, **limits)
    return create_server(profile, account, log_folder=tmp_path)


def call(srv, **args):
    tool = args.pop("tool", "manage_portfolio_bidding_strategy")
    async def run():
        async with Client(srv) as client:
            return json.loads((await client.call_tool(tool, args)).content[0].text)
    return asyncio.run(run())


def execute(srv, **args):
    token = call(srv, **args)["confirm_token"]
    return call(srv, **args, confirm=True, confirm_token=token)


def logged(tmp_path):
    return [json.loads(line) for line in (tmp_path / "changes.jsonl").read_text().splitlines()]


CREATE = {"action": "create", "name": "Coffee together", "strategy": "MAXIMIZE_CONVERSIONS", "campaign_ids": ["1", "2"]}


def test_needs_level_full(account, tmp_path):
    with pytest.raises(ToolError, match="needs full"):
        call(server(account, tmp_path, level="standard"), **CREATE)


def test_create_dry_run(account, tmp_path):
    out = call(server(account, tmp_path), **CREATE)
    assert [c["field"] for c in out["changes"]] == ["portfolio", "bidding_strategy", "bidding_strategy"]
    assert "budget stays 10.00 EUR/day" in out["changes"][1]["object"]
    assert out["changes"][1]["old"] == {"strategy": "TARGET_SPEND", "cpc_bid_ceiling": 1.0}
    text = " ".join(out["warnings"])
    assert "8 in the portfolio" in text and "'Coffee DE' 6" in text and "under about 15" in text
    assert "keeps its own budget" in text
    assert account.mutations == [(True, "GoogleAdsService", 3)]


@pytest.mark.parametrize("args,message", [
    ({**CREATE, "campaign_ids": ["1", "3"]}, "Performance Max does not"),
    ({**CREATE, "name": "coffee shared"}, "exists already"),
    ({**CREATE, "strategy": "TARGET_CPA"}, "needs target_cpa"),
    ({**CREATE, "strategy": "TARGET_SPEND", "target_cpa": 10}, "target_cpa only goes with"),
    ({"action": "assign", "portfolio_id": "901", "campaign_ids": ["1"]}, "not found"),
    ({"action": "unassign", "campaign_ids": ["1"], "standard_strategy": "MANUAL_CPC"}, "does not use a portfolio"),
])
def test_refusals(account, tmp_path, args, message):
    with pytest.raises(ToolError, match=message):
        call(server(account, tmp_path), **args)
    assert account.mutations == []


def test_max_cpc_profile_limit(account, tmp_path):
    with pytest.raises(ToolError, match="cpc_bid_ceiling is required"):
        call(server(account, tmp_path, max_cpc=2.0), **{**CREATE, "strategy": "TARGET_SPEND"})


def test_aligned_portfolio_is_left_alone(account, tmp_path):
    account.portfolio["bidding_strategy.aligned_campaign_budget_id"] = 77
    with pytest.raises(ToolError, match="tied to shared budget 77"):
        call(server(account, tmp_path), action="assign", portfolio_id="900", campaign_ids=["1"])
    with pytest.raises(ToolError, match="tied to shared budget 77"):
        call(server(account, tmp_path), action="remove", portfolio_id="900")


def test_warnings_for_goals_and_shared_budgets(account, tmp_path):
    account.goals[2] = [("PHONE_CALL_LEAD", "CALL_FROM_ADS")]
    account.campaigns[1]["campaign_budget.explicitly_shared"] = True
    account.shared_users = {1: f"customers/{DEMO}/campaignBudgets/1", 7: f"customers/{DEMO}/campaignBudgets/1"}
    text = " ".join(call(server(account, tmp_path), **CREATE)["warnings"])
    assert "different conversion goals" in text
    assert "shares its budget with campaigns outside" in text


def test_create_execute_and_revert(account, tmp_path):
    srv = server(account, tmp_path)
    execute(srv, **CREATE)
    entries = logged(tmp_path)
    assert [e["object_type"] for e in entries] == ["portfolio_bidding_strategy", "campaign_bidding_strategy",
                                                   "campaign_bidding_strategy"]
    with pytest.raises(ToolError, match="cannot be reverted"):
        call(srv, tool="revert_change", change_id=entries[0]["change_id"])
    account.portfolio["bidding_strategy.name"] = "Coffee together"
    account.join(1, 2)
    back = call(srv, tool="revert_change", change_id=entries[1]["change_id"])
    assert back["changes"][0]["new"] == {"strategy": "TARGET_SPEND", "cpc_bid_ceiling": 1.0}


def test_unassign_and_revert_back_into_portfolio(account, tmp_path):
    account.join(1)
    srv = server(account, tmp_path)
    out = execute(srv, action="unassign", campaign_ids=["1"], standard_strategy="MAXIMIZE_CONVERSIONS")
    assert out["changes"][0]["old"]["portfolio"] == "Coffee shared"
    account.campaigns[1].update({"campaign.bidding_strategy": None, "campaign.bidding_strategy_type": "MAXIMIZE_CONVERSIONS"})
    back = call(srv, tool="revert_change", change_id=out["changes"][0]["change_id"])
    assert back["changes"][0]["new"]["portfolio"] == "Coffee shared"


def test_assign_skips_members(account, tmp_path):
    account.join(1)
    out = call(server(account, tmp_path), action="assign", portfolio_id="900", campaign_ids=["1", "2"])
    assert len(out["changes"]) == 1 and any("already uses this portfolio" in w for w in out["warnings"])


def test_update_step_limit_and_revert(account, tmp_path):
    account.join(1, 2)
    srv = server(account, tmp_path)
    with pytest.raises(ToolError, match="max_target_change_percent"):
        call(srv, action="update", portfolio_id="900", target_cpa=30)
    out = execute(srv, action="update", portfolio_id="900", target_cpa=22)
    assert out["changes"][0]["new"]["target_cpa"] == 22.0
    assert any("'Coffee DE'" in w for w in out["warnings"])
    account.portfolio["bidding_strategy.maximize_conversions.target_cpa_micros"] = 22_000_000
    back = call(srv, tool="revert_change", change_id=out["changes"][0]["change_id"])
    assert back["changes"][0]["new"]["target_cpa"] == 20.0


def test_remove_only_when_unused(account, tmp_path):
    account.join(1)
    with pytest.raises(ToolError, match="still used by 1"):
        call(server(account, tmp_path), action="remove", portfolio_id="900")
    account.portfolio["bidding_strategy.non_removed_campaign_count"] = 0
    out = call(server(account, tmp_path), action="remove", portfolio_id="900")
    assert any("final" in w for w in out["warnings"])


def test_list_bidding_strategies(account, tmp_path):
    account.join(1)
    out = call(server(account, tmp_path), tool="list_bidding_strategies")
    item = out["portfolio_bidding_strategies"][0]
    assert item["target_cpa"] == 20.0 and item["campaigns"][0]["name"] == "Coffee DE"
    assert item["aligned_shared_budget_id"] is None
