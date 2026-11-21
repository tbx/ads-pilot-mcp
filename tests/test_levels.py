"""Offline tests for access levels, set_bidding_strategy and the conversion action tools."""

import asyncio
import json
from datetime import datetime

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from ads_pilot_mcp.ads import MutateResult
from ads_pilot_mcp.config import ConfigError, Profile, load_profile
from ads_pilot_mcp.server import create_server

DEMO = "1234567890"
OTHER = "2223334444"


def action(id_, name, type_="WEBPAGE", status="ENABLED", primary=True, category="SUBMIT_LEAD_FORM", origin="WEBSITE"):
    return {"conversion_action.resource_name": f"customers/{DEMO}/conversionActions/{id_}", "conversion_action.id": id_,
            "conversion_action.name": name, "conversion_action.type": type_, "conversion_action.category": category,
            "conversion_action.origin": origin, "conversion_action.status": status,
            "conversion_action.primary_for_goal": primary, "conversion_action.include_in_conversions_metric": primary,
            "conversion_action.counting_type": "ONE_PER_CLICK", "conversion_action.value_settings.default_value": 100.0,
            "conversion_action.value_settings.always_use_default_value": True,
            "conversion_action.click_through_lookback_window_days": 90,
            "conversion_action.view_through_lookback_window_days": 1,
            "conversion_action.phone_call_duration_seconds": 60, "conversion_action.owner_customer": f"customers/{DEMO}",
            "conversion_action.google_analytics_4_settings.property_name": "",
            "conversion_action.google_analytics_4_settings.event_name": ""}


class FakeAccount:
    def __init__(self):
        self._client = GoogleAdsClient(credentials=AnonymousCredentials(), version="v26", use_proto_plus=True)
        self.mutations = []
        self.campaign = {"channel": "SEARCH", "type": "TARGET_SPEND", "portfolio": None, "ceiling": 1_200_000, "cpa": 0}
        self.conversions_30d = 8.0
        self.actions = {a["conversion_action.id"]: a for a in [
            action(1, "Lead form"), action(2, "Old lead form", primary=True),
            action(3, "Old GA4 event", "GOOGLE_ANALYTICS_4_CUSTOM", status="HIDDEN", primary=False, category="CONTACT"),
            action(4, "Old UA goal", "UNIVERSAL_ANALYTICS_GOAL", status="HIDDEN", category="CONTACT"),
            action(5, "Local actions", "GOOGLE_HOSTED", origin="GOOGLE_HOSTED", category="GET_DIRECTIONS")]}
        self.counts = {1: 80.0}
        self.bidding_on_goal = "MAXIMIZE_CONVERSIONS"

    def client(self, login_customer_id=None):
        return self._client

    def search(self, customer_id, query, login_customer_id=None, max_rows=200):
        if "FROM campaign WHERE campaign.id" in query and "advertising_channel_type" in query:
            c = self.campaign
            return [{"campaign.resource_name": f"customers/{customer_id}/campaigns/555", "campaign.name": "Coffee DE",
                     "campaign.status": "ENABLED", "campaign.advertising_channel_type": c["channel"],
                     "campaign.bidding_strategy": c["portfolio"], "campaign.bidding_strategy_type": c["type"],
                     "campaign.target_spend.cpc_bid_ceiling_micros": c["ceiling"],
                     "campaign.maximize_conversions.target_cpa_micros": c["cpa"],
                     "campaign.maximize_conversion_value.target_roas": 0.0, "campaign.target_cpa.target_cpa_micros": 0,
                     "campaign.target_roas.target_roas": 0.0, "campaign.target_impression_share.location": "UNSPECIFIED",
                     "campaign.target_impression_share.location_fraction_micros": 0,
                     "campaign.target_impression_share.cpc_bid_ceiling_micros": 0, "customer.currency_code": "EUR"}], False
        if query.startswith("SELECT metrics.conversions FROM campaign"):
            return [{"metrics.conversions": self.conversions_30d}], False
        if "FROM change_event" in query:
            return [], False
        if "FROM conversion_action WHERE conversion_action.resource_name IN" in query:
            return [a for a in self.actions.values() if f"'{a['conversion_action.resource_name']}'" in query], False
        if "FROM conversion_action WHERE conversion_action.status = 'ENABLED'" in query:
            return [a for a in self.actions.values() if a["conversion_action.status"] == "ENABLED"
                    and a["conversion_action.primary_for_goal"]], False
        if "FROM conversion_action" in query:
            return list(self.actions.values()), False
        if "FROM customer WHERE segments.date BETWEEN" in query:
            return [{"segments.conversion_action": f"customers/{DEMO}/conversionActions/{i}", "metrics.conversions": n,
                     "metrics.all_conversions": n} for i, n in self.counts.items()], False
        if "FROM campaign_conversion_goal" in query:
            return [{"campaign.id": 556, "campaign.name": "Coffee PMax", "campaign.status": "ENABLED",
                     "campaign.bidding_strategy_type": self.bidding_on_goal,
                     "campaign_conversion_goal.category": "SUBMIT_LEAD_FORM",
                     "campaign_conversion_goal.origin": "WEBSITE"}], False
        raise AssertionError(f"unexpected query: {query}")

    def mutate(self, customer_id, service, method, operations, login_customer_id=None, validate_only=True,
               partial_failure=False):
        self.mutations.append((validate_only, service, len(operations)))
        return MutateResult(results=[f"r{i}" for i in range(len(operations))])


@pytest.fixture
def account():
    return FakeAccount()


def make_server(account, tmp_path, level="full", **limits):
    profile = Profile("fjord", {"client_id": "x"}, default_customer_id=DEMO, account_levels={DEMO: level, OTHER: "standard"},
                      **limits)
    return create_server(profile, account, log_folder=tmp_path)


def call(server, tool, **args):
    async def run():
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return json.loads(result.content[0].text)
    return asyncio.run(run())


def execute(server, tool, **args):
    token = call(server, tool, **args)["confirm_token"]
    return call(server, tool, **args, confirm=True, confirm_token=token)


# ---- access levels ---------------------------------------------------------------

def test_profile_reads_levels(tmp_path, monkeypatch):
    (tmp_path / "p.yaml").write_text('auth: {client_id: x}\nallowed_customer_ids: ["111-111-1111"]\n'
                                     'accounts:\n  "123-456-7890": standard\n  "222-333-4444": read_only\n')
    monkeypatch.setenv("ADS_PILOT_PROFILES_DIR", str(tmp_path))
    profile = load_profile("p")
    assert profile.level_for("123-456-7890") == "standard"
    assert profile.level_for("111-111-1111") == "full"  # older profiles: allowlist means full
    assert profile.level_for("222-333-4444") == "read_only"
    assert profile.level_for("999-999-9999") == "read_only"
    assert profile.writable_accounts() == {"111-111-1111": "full", "123-456-7890": "standard"}


def test_profile_rejects_unknown_level(tmp_path, monkeypatch):
    (tmp_path / "p.yaml").write_text('auth: {client_id: x}\naccounts:\n  "123-456-7890": admin\n')
    monkeypatch.setenv("ADS_PILOT_PROFILES_DIR", str(tmp_path))
    with pytest.raises(ConfigError, match="read_only, standard, full"):
        load_profile("p")


def test_standard_account_cannot_use_full_tools(account, tmp_path):
    server = make_server(account, tmp_path, level="standard")
    with pytest.raises(ToolError, match="needs full"):
        call(server, "set_bidding_strategy", campaign_id="555", strategy="MAXIMIZE_CONVERSIONS")
    with pytest.raises(ToolError, match="needs full"):
        call(server, "set_conversion_action_settings", conversion_action_id="1", default_value=50)
    assert account.mutations == []


def test_unlisted_account_is_read_only(account, tmp_path):
    with pytest.raises(ToolError, match="read_only"):
        call(make_server(account, tmp_path), "set_bidding_strategy", campaign_id="555", strategy="MANUAL_CPC",
             customer_id="999-999-9999")


def test_show_profile_lists_levels(account, tmp_path):
    out = call(make_server(account, tmp_path), "show_profile")
    assert out["account_levels"] == {"123-456-7890": "full", "222-333-4444": "standard"}


def test_revert_needs_level_of_original_tool(account, tmp_path):
    execute(make_server(account, tmp_path), "set_bidding_strategy", campaign_id="555", strategy="MAXIMIZE_CONVERSIONS")
    change_id = json.loads((tmp_path / "changes.jsonl").read_text().splitlines()[-1])["change_id"]
    with pytest.raises(ToolError, match="needs full"):
        call(make_server(account, tmp_path, level="standard"), "revert_change", change_id=change_id)


# ---- set_bidding_strategy ---------------------------------------------------------

def test_strategy_dry_run_shows_both_states_and_warns(account, tmp_path):
    out = call(make_server(account, tmp_path), "set_bidding_strategy", campaign_id="555", strategy="MAXIMIZE_CONVERSIONS")
    change = out["changes"][0]
    assert change["old"] == {"strategy": "TARGET_SPEND", "cpc_bid_ceiling": 1.2}
    assert change["new"] == {"strategy": "MAXIMIZE_CONVERSIONS", "target_cpa": None}
    text = " ".join(out["warnings"])
    assert "learning phase" in text and "8 primary conversions" in text
    assert account.mutations == [(True, "CampaignService", 1)]


def test_strategy_warns_about_recent_logged_change(account, tmp_path):
    (tmp_path / "changes.jsonl").write_text(json.dumps({"change_id": "a1", "tool": "set_bidding_target", "object_id": "555",
                                                        "time": datetime.now().astimezone().isoformat()}) + "\n")
    out = call(make_server(account, tmp_path), "set_bidding_strategy", campaign_id="555", strategy="MANUAL_CPC")
    assert any("learning phase is probably still running" in w for w in out["warnings"])


def test_maximize_clicks_without_ceiling(account, tmp_path):
    account.campaign.update(type="MAXIMIZE_CONVERSIONS", ceiling=None)
    out = call(make_server(account, tmp_path), "set_bidding_strategy", campaign_id="555", strategy="TARGET_SPEND")
    assert any("any price per click" in w for w in out["warnings"])
    with pytest.raises(ToolError, match="cpc_bid_ceiling is required"):
        call(make_server(account, tmp_path, max_cpc=2.0), "set_bidding_strategy", campaign_id="555", strategy="TARGET_SPEND")
    with pytest.raises(ToolError, match="above this profile's max_cpc"):
        call(make_server(account, tmp_path, max_cpc=2.0), "set_bidding_strategy", campaign_id="555",
             strategy="TARGET_SPEND", cpc_bid_ceiling=3.0)


@pytest.mark.parametrize("setup,args,message", [
    ({"channel": "PERFORMANCE_MAX", "type": "MAXIMIZE_CONVERSIONS"}, {"strategy": "TARGET_SPEND"}, "only supports"),
    ({"portfolio": "customers/1/biddingStrategies/9"}, {"strategy": "MANUAL_CPC"}, "portfolio"),
    ({"channel": "DISPLAY"}, {"strategy": "MANUAL_CPC"}, "only for Search and"),
    ({}, {"strategy": "MANUAL_CPC", "target_cpa": 20}, "target_cpa only goes with"),
    ({"type": "MAXIMIZE_CONVERSIONS", "cpa": 20_000_000}, {"strategy": "MAXIMIZE_CONVERSIONS", "target_cpa": 25},
     "use set_bidding_target"),
])
def test_strategy_refusals(account, tmp_path, setup, args, message):
    account.campaign.update(setup)
    with pytest.raises(ToolError, match=message):
        call(make_server(account, tmp_path), "set_bidding_strategy", campaign_id="555", **args)
    assert account.mutations == []


def test_allowed_bidding_strategies(account, tmp_path):
    server = make_server(account, tmp_path, allowed_bidding_strategies=frozenset({"MAXIMIZE_CONVERSIONS"}))
    with pytest.raises(ToolError, match="allows only these strategies"):
        call(server, "set_bidding_strategy", campaign_id="555", strategy="MANUAL_CPC")


def test_strategy_execute_and_revert(account, tmp_path):
    server = make_server(account, tmp_path)
    out = execute(server, "set_bidding_strategy", campaign_id="555", strategy="MAXIMIZE_CONVERSIONS")
    entry = json.loads((tmp_path / "changes.jsonl").read_text().splitlines()[-1])
    assert entry["old"] == {"strategy": "TARGET_SPEND", "cpc_bid_ceiling": 1.2} and entry["object_id"] == "555"
    account.campaign.update(type="MAXIMIZE_CONVERSIONS", ceiling=None)  # the account now has the new state
    back = call(server, "revert_change", change_id=out["changes"][0]["change_id"])
    assert back["changes"][0]["new"] == {"strategy": "TARGET_SPEND", "cpc_bid_ceiling": 1.2}
    account.campaign.update(type="MANUAL_CPC")  # someone changed it by hand in the meantime
    with pytest.raises(ToolError, match="changed since"):
        call(server, "revert_change", change_id=out["changes"][0]["change_id"])


# ---- conversion actions -----------------------------------------------------------

def test_list_conversion_actions(account, tmp_path):
    out = call(make_server(account, tmp_path), "list_conversion_actions")
    by_id = {a["id"]: a for a in out["conversion_actions"]}
    assert by_id[1]["conversions"] == 80.0 and by_id[1]["mutable"]
    assert not by_id[4]["mutable"] and "Universal Analytics" in by_id[4]["not_mutable_because"]
    assert not by_id[5]["mutable"]
    assert out["summary"]["HIDDEN"] == 2 and "SUBMIT_LEAD_FORM (WEBSITE)" in out["summary"]["goals_with_primary_actions"]


@pytest.mark.parametrize("ids,status,message", [
    (["1"], "HIDDEN", "does not allow hiding"),
    (["4"], "REMOVED", "Universal Analytics"),
    (["5"], "ENABLED", "Google-owned"),
    (["1"], "REMOVED", "had conversions"),
])
def test_conversion_status_refusals(account, tmp_path, ids, status, message):
    with pytest.raises(ToolError, match=message):
        call(make_server(account, tmp_path), "set_conversion_action_status", conversion_action_ids=ids, status=status)
    assert account.mutations == []


def test_remove_without_data_is_final_and_not_revertible(account, tmp_path):
    server = make_server(account, tmp_path)
    out = call(server, "set_conversion_action_status", conversion_action_ids=["3"], status="REMOVED")
    assert any("final" in w for w in out["warnings"])
    done = execute(server, "set_conversion_action_status", conversion_action_ids=["3"], status="REMOVED")
    with pytest.raises(ToolError, match="final"):
        call(server, "revert_change", change_id=done["changes"][0]["change_id"])


def test_goal_loss_blocks_smart_bidding(account, tmp_path):
    account.counts = {}
    server = make_server(account, tmp_path)
    # Removing one of two primary lead actions is fine.
    assert call(server, "set_conversion_action_status", conversion_action_ids=["2"], status="REMOVED")["mode"] == "dry_run"
    # Removing both leaves the goal empty while a smart bidding campaign bids on it.
    with pytest.raises(ToolError, match="Coffee PMax"):
        call(server, "set_conversion_action_status", conversion_action_ids=["1", "2"], status="REMOVED")
    out = call(server, "set_conversion_action_status", conversion_action_ids=["1", "2"], status="REMOVED",
               allow_goal_loss=True)
    assert any("no enabled primary conversion action left" in w for w in out["warnings"])
    # Without smart bidding it is a warning only.
    account.bidding_on_goal = "TARGET_SPEND"
    assert call(server, "set_conversion_action_status", conversion_action_ids=["1", "2"], status="REMOVED")["warnings"]


def test_settings_secondary_blocked_for_last_primary(account, tmp_path):
    del account.actions[2]
    with pytest.raises(ToolError, match="Blocked"):
        call(make_server(account, tmp_path), "set_conversion_action_settings", conversion_action_id="1",
             primary_for_goal=False)


def test_ga4_counting_type_is_refused(account, tmp_path):
    with pytest.raises(ToolError, match="comes from Google Analytics"):
        call(make_server(account, tmp_path), "set_conversion_action_settings", conversion_action_id="3",
             counting_type="MANY_PER_CLICK")
    with pytest.raises(ToolError, match="only works for call actions"):
        call(make_server(account, tmp_path), "set_conversion_action_settings", conversion_action_id="1",
             phone_call_duration_seconds=60)


def test_settings_execute_log_and_revert(account, tmp_path):
    server = make_server(account, tmp_path)
    out = execute(server, "set_conversion_action_settings", conversion_action_id="1", default_value=150,
                  counting_type="MANY_PER_CLICK")
    assert [c["field"] for c in out["changes"]] == ["default_value", "counting_type"]
    assert any("not recalculated" in w for w in out["warnings"])
    account.actions[1]["conversion_action.value_settings.default_value"] = 150.0
    back = call(server, "revert_change", change_id=out["changes"][0]["change_id"])
    assert back["changes"][0]["old"] == 150.0 and back["changes"][0]["new"] == 100.0
