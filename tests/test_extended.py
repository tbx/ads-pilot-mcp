"""Offline tests for priorities 4 to 6 and the read improvements. A fake account stands in for Google Ads."""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from ads_pilot_mcp import reports
from ads_pilot_mcp.ads import AdsError, MutateResult
from ads_pilot_mcp.config import Profile
from ads_pilot_mcp.server import create_server

DEMO = "1234567890"
HEADS = [f"Headline number {i}" for i in range(1, 6)]


class FakeAccount:
    def __init__(self):
        self._client = GoogleAdsClient(credentials=AnonymousCredentials(), version="v26", use_proto_plus=True)
        self.mutations = []
        self.brand_guidelines = False
        self.themes = {"espresso beans": "customers/1/assetGroupSignals/321~1"}
        self.strategy = {"type": "TARGET_CPA", "portfolio": None, "cpa": 20_000_000, "roas": 0.0}
        self.bid_type = "MANUAL_CPC"
        self.status = "ENABLED"
        self.budget = 40_000_000

    def client(self, login_customer_id=None):
        return self._client

    def currency(self, customer_id, login_customer_id=None):
        return "EUR"

    def search_fields(self, query):
        if "WHERE name = 'campaign'" in query:
            return [{"name": "campaign", "category": "RESOURCE", "selectable_with": ["segments.date", "metrics.clicks",
                                                                                      "bidding_strategy"]}]
        if "WHERE name = 'segments.ad_network_type'" in query:
            return [{"name": "segments.ad_network_type", "data_type": "ENUM", "filterable": True, "sortable": True,
                     "enum_values": ["SEARCH", "YOUTUBE"]}]
        if "LIKE 'campaign.%'" in query:
            return [{"name": n, "category": "ATTRIBUTE", "data_type": "STRING", "selectable": True, "filterable": True,
                     "sortable": True, "enum_values": []} for n in ("campaign.name", "campaign.start_date_time", "campaign.status")]
        return []

    def search(self, customer_id, query, login_customer_id=None, max_rows=200):
        if "FROM asset_group WHERE asset_group.id" in query:
            return [{"asset_group.resource_name": f"customers/{customer_id}/assetGroups/321", "asset_group.name": "Espresso",
                     "asset_group.status": "ENABLED", "campaign.id": 555, "campaign.resource_name": f"customers/{customer_id}/campaigns/555",
                     "campaign.name": "Coffee PMax", "campaign.status": "ENABLED", "campaign.advertising_channel_type": "PERFORMANCE_MAX",
                     "campaign.brand_guidelines_enabled": self.brand_guidelines}], False
        if "FROM asset_group_asset" in query:
            return [{"asset_group_asset.resource_name": f"customers/{customer_id}/assetGroupAssets/321~{i}~HEADLINE",
                     "asset_group_asset.field_type": "HEADLINE", "asset.resource_name": f"customers/{customer_id}/assets/{i}",
                     "asset.text_asset.text": t} for i, t in enumerate(HEADS)], False
        if "FROM campaign_asset" in query:
            return [{"campaign_asset.resource_name": "customers/1/campaignAssets/555~9~BUSINESS_NAME",
                     "asset.resource_name": "customers/1/assets/9", "asset.text_asset.text": "Fjord Coffee"}], False
        if "FROM asset WHERE asset.type = TEXT" in query:
            return [], False
        if "FROM asset_group_signal" in query:
            return [{"asset_group_signal.resource_name": rn, "asset_group_signal.search_theme.text": t}
                    for t, rn in self.themes.items()], False
        if "campaign.bidding_strategy_type, campaign.target_cpa" in query:
            s = self.strategy
            return [{"campaign.resource_name": f"customers/{customer_id}/campaigns/555", "campaign.name": "Coffee DE",
                     "campaign.status": "ENABLED", "campaign.bidding_strategy": s["portfolio"],
                     "campaign.bidding_strategy_type": s["type"], "campaign.target_cpa.target_cpa_micros": s["cpa"],
                     "campaign.maximize_conversions.target_cpa_micros": s["cpa"], "campaign.target_roas.target_roas": s["roas"],
                     "campaign.maximize_conversion_value.target_roas": s["roas"], "customer.currency_code": "EUR"}], False
        if "FROM ad_group_criterion WHERE ad_group_criterion.resource_name IN" in query:
            return [{"ad_group_criterion.resource_name": f"customers/{customer_id}/adGroupCriteria/111~222",
                     "ad_group_criterion.status": self.status, "ad_group_criterion.keyword.text": "espresso",
                     "ad_group_criterion.keyword.match_type": "EXACT", "ad_group_criterion.negative": False,
                     "ad_group_criterion.cpc_bid_micros": 1_000_000, "ad_group_criterion.type": "KEYWORD",
                     "ad_group.cpc_bid_micros": 800_000, "ad_group.name": "Espresso", "ad_group.status": "ENABLED",
                     "campaign.name": "Coffee DE", "campaign.status": "ENABLED", "campaign.bidding_strategy_type": self.bid_type,
                     "customer.currency_code": "EUR"}], False
        if "FROM campaign_budget WHERE" in query:
            return [{"campaign_budget.amount_micros": self.budget, "customer.currency_code": "EUR"}], False
        if "FROM campaign WHERE campaign.status IN" in query:
            return [{"campaign.id": 555, "campaign.name": "Coffee DE", "campaign.advertising_channel_type": "SEARCH",
                     "campaign.status": "ENABLED", "campaign.bidding_strategy_type": "TARGET_CPA",
                     "campaign_budget.amount_micros": 40_000_000, "metrics.cost_micros": 100_000_000, "metrics.clicks": 50,
                     "metrics.impressions": 1000, "metrics.conversions": 4.0, "metrics.conversions_value": 400.0}], False
        if "FROM search_term_view" in query:
            return [{"search_term_view.search_term": "cheap espresso", "search_term_view.status": "NONE", "campaign.id": 555,
                     "campaign.name": "Coffee DE", "ad_group.name": "Espresso", "metrics.cost_micros": 30_000_000,
                     "metrics.clicks": 10, "metrics.impressions": 100, "metrics.conversions": 0.0,
                     "metrics.conversions_value": 0.0}], False
        if "FROM campaign_search_term_view" in query:
            return [{"campaign_search_term_view.search_term": "coffee beans", "campaign.id": 556, "campaign.name": "Coffee PMax",
                     "metrics.cost_micros": 50_000_000, "metrics.clicks": 20, "metrics.impressions": 300,
                     "metrics.conversions": 2.0, "metrics.conversions_value": 80.0}], False
        raise AssertionError(f"unexpected query: {query}")

    def mutate(self, customer_id, service, method, operations, login_customer_id=None, validate_only=True,
               partial_failure=False):
        self.mutations.append((validate_only, service, len(operations)))
        return MutateResult(results=[f"r{i}" for i in range(len(operations))])


@pytest.fixture
def account():
    return FakeAccount()


def make_server(account, tmp_path, **limits):
    profile = Profile("fjord", {"client_id": "x"}, default_customer_id=DEMO, allowed_customer_ids=frozenset({DEMO}),
                      **limits)
    return create_server(profile, account, log_folder=tmp_path)


@pytest.fixture
def server(account, tmp_path):
    return make_server(account, tmp_path)


def call(server, tool, **args):
    async def run():
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return json.loads(result.content[0].text)
    return asyncio.run(run())


CASES = [
    ("update_asset_group_text_assets", {"asset_group_id": "321", "headlines": HEADS[:-1] + ["Brand new headline"]}),
    ("set_search_themes", {"asset_group_id": "321", "add": ["coffee subscription"]}),
    ("set_bidding_target", {"campaign_id": "555", "target_cpa": 22}),
    ("set_keyword_bid", {"object_type": "keyword", "ids": ["111~222"], "max_cpc": 1.2}),
]


# ---- shared rules for each new write tool -------------------------------------

@pytest.mark.parametrize("tool,args", CASES)
def test_dry_run_changes_nothing(server, account, tmp_path, tool, args):
    assert call(server, tool, **args)["mode"] == "dry_run"
    assert all(v for v, *_ in account.mutations) and not (tmp_path / "changes.jsonl").exists()


@pytest.mark.parametrize("tool,args", CASES)
def test_wrong_token_and_other_account_refused(server, account, tool, args):
    call(server, tool, **args)
    with pytest.raises(ToolError, match="confirm_token"):
        call(server, tool, **args, confirm=True, confirm_token="1.bad")
    with pytest.raises(ToolError, match="read_only"):
        call(server, tool, **args, customer_id="111-222-3333")
    assert all(v for v, *_ in account.mutations)


@pytest.mark.parametrize("tool,args", CASES)
def test_execute_logs_with_change_id(server, tmp_path, tool, args):
    token = call(server, tool, **args)["confirm_token"]
    result = call(server, tool, **args, confirm=True, confirm_token=token)
    entry = json.loads((tmp_path / "changes.jsonl").read_text().splitlines()[-1])
    assert result["mode"] == "executed" and entry["tool"] == tool and entry["change_id"]


# ---- priority 4: Performance Max ------------------------------------------------

def test_text_assets_create_link_and_unlink(server, account):
    result = call(server, "update_asset_group_text_assets", asset_group_id="321", headlines=HEADS[:-1] + ["Brand new headline"])
    assert result["changes"][0]["old"] == HEADS and result["changes"][0]["new"][-1] == "Brand new headline"
    assert account.mutations[-1] == (True, "GoogleAdsService", 3)  # create asset, link it, unlink the old one


def test_text_asset_limits(server):
    with pytest.raises(ToolError, match="between 3 and 15"):
        call(server, "update_asset_group_text_assets", asset_group_id="321", headlines=["One", "Two"])
    with pytest.raises(ToolError, match="limit is 30"):
        call(server, "update_asset_group_text_assets", asset_group_id="321", headlines=HEADS + ["x" * 31])
    with pytest.raises(ToolError, match="limit is 25"):
        call(server, "update_asset_group_text_assets", asset_group_id="321", business_name="x" * 26)


def test_business_name_goes_to_campaign_with_brand_guidelines(server, account):
    account.brand_guidelines = True
    result = call(server, "update_asset_group_text_assets", asset_group_id="321", business_name="Fjord Roasters")
    assert "(brand guidelines)" in result["changes"][0]["object"] and result["changes"][0]["old"] == ["Fjord Coffee"]


def test_search_themes_skip_and_remove(server):
    result = call(server, "set_search_themes", asset_group_id="321", add=["Espresso Beans"], remove=["espresso beans", "nope"])
    assert [c["new"] for c in result["changes"]] == ["removed"]
    assert any("already in" in w for w in result["warnings"]) and any("nothing to remove" in w for w in result["warnings"])


# ---- priority 5: bids -----------------------------------------------------------

def test_bidding_target_limits(server, account):
    assert call(server, "set_bidding_target", campaign_id="555", target_cpa=24)["changes"][0]["old"] == "20.00 EUR"
    with pytest.raises(ToolError, match="at most 30%"):
        call(server, "set_bidding_target", campaign_id="555", target_cpa=30)
    account.strategy["portfolio"] = "customers/1/biddingStrategies/9"
    with pytest.raises(ToolError, match="portfolio"):
        call(server, "set_bidding_target", campaign_id="555", target_cpa=22)


def test_bidding_target_never_switches_strategy(server, account):
    with pytest.raises(ToolError, match="not a target ROAS strategy"):
        call(server, "set_bidding_target", campaign_id="555", target_roas_percent=400)
    account.strategy.update(type="MAXIMIZE_CONVERSIONS", cpa=0)
    with pytest.raises(ToolError, match="without a target CPA"):
        call(server, "set_bidding_target", campaign_id="555", target_cpa=22)


def test_max_target_cpa_from_profile(account, tmp_path):
    with pytest.raises(ToolError, match="max_target_cpa"):
        call(make_server(account, tmp_path, max_target_cpa=21), "set_bidding_target", campaign_id="555", target_cpa=22)


def test_keyword_bid_needs_manual_cpc_and_respects_cap(server, account, tmp_path):
    with pytest.raises(ToolError, match="max_cpc of 1.00"):
        call(make_server(account, tmp_path, max_cpc=1.0), "set_keyword_bid", object_type="keyword", ids=["111~222"], max_cpc=1.5)
    account.bid_type = "TARGET_SPEND"
    with pytest.raises(ToolError, match="manual CPC"):
        call(server, "set_keyword_bid", object_type="keyword", ids=["111~222"], max_cpc=1.2)


# ---- priority 6: revert ---------------------------------------------------------

def test_revert_round_trip(server, account, tmp_path):
    args = {"object_type": "keyword", "ids": ["111~222"], "status": "PAUSED"}
    token = call(server, "set_status", **args)["confirm_token"]
    change_id = call(server, "set_status", **args, confirm=True, confirm_token=token)["changes"][0]["change_id"]
    account.status = "PAUSED"  # the fake account now reflects the change
    dry = call(server, "revert_change", change_id=change_id)
    assert dry["changes"][0]["old"] == "PAUSED" and dry["changes"][0]["new"] == "ENABLED"
    call(server, "revert_change", change_id=change_id, confirm=True, confirm_token=dry["confirm_token"])
    entry = json.loads((tmp_path / "changes.jsonl").read_text().splitlines()[-1])
    assert entry["tool"] == "revert_change" and entry["reverted_change_id"] == change_id
    assert entry["original_tool"] == "set_status"


def test_revert_refuses_when_changed_meanwhile(server, account, tmp_path):
    # A log entry says 40 -> 45, but the account still has 40: someone changed it back meanwhile.
    (tmp_path / "changes.jsonl").write_text(json.dumps({
        "change_id": "abc123", "profile": "fjord", "customer_id": "123-456-7890", "tool": "set_campaign_budget",
        "object": "daily budget of campaign 'Coffee DE'", "field": "daily_budget", "old": "40.00 EUR", "new": "45.00 EUR",
        "resource": "customers/1234567890/campaignBudgets/7", "object_type": "campaign_budget", "object_id": "555"}) + "\n")
    with pytest.raises(ToolError, match="value changed since.*45.00 EUR.*40.00 EUR"):
        call(server, "revert_change", change_id="abc123")


def test_revert_unknown_and_not_revertible(server, tmp_path):
    with pytest.raises(ToolError, match="No change with change_id"):
        call(server, "revert_change", change_id="nope")
    (tmp_path / "changes.jsonl").write_text(json.dumps({
        "change_id": "k1", "profile": "fjord", "customer_id": "123-456-7890", "tool": "add_keywords",
        "object": "keyword", "field": "keyword", "old": None, "new": "added", "resource": "r"}) + "\n")
    with pytest.raises(ToolError, match="cannot be reverted cleanly"):
        call(server, "revert_change", change_id="k1")


# ---- reading --------------------------------------------------------------------

def test_add_money():
    rows = reports.add_money([{"metrics.cost_micros": 12_340_000, "campaign.name": "x"}])
    assert rows[0]["metrics.cost"] == 12.34


def test_field_hints(account):
    hints = reports.field_hints(account, "query_error=UNRECOGNIZED_FIELD | Unrecognized field in the query: 'campaign.start_date'.")
    assert "campaign.start_date_time" in hints[0]


def test_describe_resource_and_field(server):
    result = call(server, "describe_resource", resource="campaign")
    assert result["segments"] == ["segments.date"] and result["metrics"] == ["metrics.clicks"]
    assert call(server, "describe_resource", resource="campaign", field="segments.ad_network_type")["enum_values"] == ["SEARCH", "YOUTUBE"]


def test_campaign_overview(server):
    result = call(server, "campaign_overview")
    assert result["profile"] == "fjord" and result["currency"] == "EUR"
    assert result["campaigns"][0]["cost_per_conversion"] == 25.0 and result["total"]["cost"] == 100.0


def test_search_terms_report_mixes_search_and_pmax_and_filters(server):
    result = call(server, "search_terms_report", min_cost_without_conversion=10)
    assert [t["search_term"] for t in result["search_terms"]] == ["cheap espresso"]
    result = call(server, "search_terms_report", sort_by="cost")
    assert [t["source"] for t in result["search_terms"]] == ["Performance Max", "Search"]


def test_report_date_validation(server):
    with pytest.raises(ToolError, match="both start_date and end_date"):
        call(server, "campaign_overview", start_date="2026-11-01")
