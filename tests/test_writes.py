"""Offline tests for the write guardrails. A fake account stands in for Google Ads."""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from ads_pilot_mcp import writes
from ads_pilot_mcp.config import Profile
from ads_pilot_mcp.server import create_server

DEMO = "1234567890"
OTHER = "1112223333"


class FakeAccount:
    """Answers GAQL queries from a small in-memory account and records mutate calls."""

    def __init__(self):
        self._client = GoogleAdsClient(credentials=AnonymousCredentials(), version="v26", use_proto_plus=True)
        self.mutations = []  # (validate_only, service, number of operations)
        self.keyword_status = "ENABLED"
        self.mobile_modifier = None
        self.schedule = [("MONDAY", 0, "ZERO", 6, "ZERO", 0.7)]
        self.budget = {"micros": 40_000_000, "shared": False, "refs": 1}

    def client(self, login_customer_id=None):
        return self._client

    def search(self, customer_id, query, login_customer_id=None, max_rows=200):
        if "FROM ad_group_criterion WHERE ad_group_criterion.resource_name IN" in query:
            return [{
                "ad_group_criterion.resource_name": f"customers/{customer_id}/adGroupCriteria/111~222",
                "ad_group_criterion.status": self.keyword_status,
                "ad_group_criterion.keyword.text": "espresso beans",
                "ad_group_criterion.keyword.match_type": "PHRASE",
                "ad_group_criterion.type": "KEYWORD",
                "ad_group.name": "Espresso", "campaign.name": "Coffee DE - Search",
            }] if "111~222" in query else [], False
        if "FROM ad_group WHERE ad_group.id" in query:
            return [{"ad_group.resource_name": f"customers/{customer_id}/adGroups/111", "ad_group.name": "Espresso",
                     "ad_group.status": "ENABLED", "campaign.name": "Coffee DE - Search"}], False
        if "FROM ad_group_criterion WHERE ad_group.id" in query:
            return [{"ad_group_criterion.keyword.text": "Espresso Beans", "ad_group_criterion.keyword.match_type": "PHRASE"}], False
        if "campaign_budget.amount_micros" in query:
            return [{"campaign.name": "Coffee DE - Search", "campaign.status": "ENABLED",
                     "campaign_budget.resource_name": f"customers/{customer_id}/campaignBudgets/777",
                     "campaign_budget.amount_micros": self.budget["micros"],
                     "campaign_budget.explicitly_shared": self.budget["shared"],
                     "campaign_budget.reference_count": self.budget["refs"], "customer.currency_code": "EUR"}], False
        if "FROM campaign WHERE campaign.id" in query:
            return [{"campaign.resource_name": f"customers/{customer_id}/campaigns/555", "campaign.name": "Coffee DE - Search",
                     "campaign.status": "ENABLED", "campaign.advertising_channel_type": "SEARCH"}], False
        if "campaign_criterion.type = DEVICE" in query:
            return [{"campaign_criterion.resource_name": f"customers/{customer_id}/campaignCriteria/555~30001",
                     "campaign_criterion.bid_modifier": self.mobile_modifier}], False
        if "campaign_criterion.type = AD_SCHEDULE" in query:
            p = "campaign_criterion.ad_schedule."
            return [{"campaign_criterion.resource_name": f"customers/{customer_id}/campaignCriteria/555~{300000 + i}",
                     "campaign_criterion.bid_modifier": mod, p + "day_of_week": day, p + "start_hour": sh,
                     p + "start_minute": sm, p + "end_hour": eh, p + "end_minute": em}
                    for i, (day, sh, sm, eh, em, mod) in enumerate(self.schedule)], False
        if "FROM ad_group_ad WHERE ad_group_ad.ad.id" in query:
            return [{
                "ad_group_ad.status": "ENABLED", "ad_group_ad.ad.type": "RESPONSIVE_SEARCH_AD",
                "ad_group_ad.ad.final_urls": ["https://example.com/old"],
                "ad_group_ad.ad.responsive_search_ad.headlines": [
                    {"text": "Brand", "pinned_field": "HEADLINE_1"}, {"text": "Espresso"}, {"text": "Fresh roast"}],
                "ad_group_ad.ad.responsive_search_ad.descriptions": [{"text": "One"}, {"text": "Two"}],
                "ad_group.name": "Espresso", "campaign.name": "Coffee DE - Search",
            }], False
        raise AssertionError(f"unexpected query: {query}")

    def mutate(self, customer_id, service, method, operations, login_customer_id=None, validate_only=True,
               partial_failure=False):
        self.mutations.append((validate_only, service, len(operations)))
        if not validate_only and service == "AdGroupCriterionService" and operations[0].update.status:
            self.keyword_status = operations[0].update.status.name
        return [f"customers/{customer_id}/x/{i}" for i in range(len(operations))]


@pytest.fixture
def account():
    return FakeAccount()


@pytest.fixture
def server(account, tmp_path):
    profile = Profile("fjord", {"client_id": "x"}, default_customer_id=DEMO, allowed_customer_ids=frozenset({DEMO}),
                      max_daily_budget=100)
    return create_server(profile, account, log_folder=tmp_path)


def call(server, tool, **args):
    async def run():
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return json.loads(result.content[0].text)
    return asyncio.run(run())


PAUSE = {"object_type": "keyword", "ids": ["111~222"], "status": "PAUSED"}


# ---- the two-step flow -----------------------------------------------------

def test_dry_run_changes_nothing(server, account, tmp_path):
    result = call(server, "set_status", **PAUSE)
    assert result["mode"] == "dry_run"
    assert result["profile"] == "fjord" and result["customer_id"] == "123-456-7890"
    assert result["changes"] == [{"object": "keyword 'espresso beans' [PHRASE] in ad group 'Espresso' (campaign 'Coffee DE - Search')",
                                  "field": "status", "old": "ENABLED", "new": "PAUSED"}]
    assert result["confirm_token"]
    assert account.mutations == [(True, "AdGroupCriterionService", 1)]  # validate_only only
    assert not (tmp_path / "changes.jsonl").exists()


def test_confirm_without_token_is_refused(server, account):
    with pytest.raises(ToolError, match="confirm_token"):
        call(server, "set_status", **PAUSE, confirm=True)
    assert all(validate_only for validate_only, *_ in account.mutations)


def test_confirm_with_wrong_token_is_refused(server, account):
    call(server, "set_status", **PAUSE)
    with pytest.raises(ToolError, match="confirm_token"):
        call(server, "set_status", **PAUSE, confirm=True, confirm_token="made-up")
    assert all(validate_only for validate_only, *_ in account.mutations)


def test_token_from_other_arguments_is_refused(server, account):
    args = {"campaign_id": "555", "device": "MOBILE"}
    token = call(server, "set_device_bid_adjustment", **args, adjustment_percent=-20)["confirm_token"]
    with pytest.raises(ToolError, match="confirm_token"):
        call(server, "set_device_bid_adjustment", **args, adjustment_percent=-30, confirm=True, confirm_token=token)
    assert all(validate_only for validate_only, *_ in account.mutations)


def test_confirm_executes_and_logs_old_and_new(server, account, tmp_path):
    token = call(server, "set_status", **PAUSE)["confirm_token"]
    result = call(server, "set_status", **PAUSE, confirm=True, confirm_token=token)
    assert result["mode"] == "executed"
    assert account.mutations[-1] == (False, "AdGroupCriterionService", 1)

    entry = json.loads((tmp_path / "changes.jsonl").read_text().splitlines()[0])
    assert entry["profile"] == "fjord" and entry["customer_id"] == "123-456-7890"
    assert entry["tool"] == "set_status" and entry["old"] == "ENABLED" and entry["new"] == "PAUSED"
    assert "espresso beans" in entry["object"] and entry["time"]

    assert call(server, "recent_changes")["changes"][0]["new"] == "PAUSED"


def test_token_is_invalid_when_account_changed_meanwhile(server, account):
    token = call(server, "set_status", **PAUSE)["confirm_token"]
    account.keyword_status = "PAUSED"  # someone paused it in the web interface
    result = call(server, "set_status", **PAUSE, confirm=True, confirm_token=token)
    assert result["mode"] == "no_change"


# ---- guardrails ------------------------------------------------------------

def test_write_to_account_outside_allowlist_is_refused(server, account):
    with pytest.raises(ToolError, match="not in allowed_customer_ids"):
        call(server, "set_status", **PAUSE, customer_id="111-222-3333")
    assert account.mutations == []


def test_reading_other_accounts_still_works(server):
    # run_gaql is not limited by the allowlist (FakeAccount answers the campaign query)
    result = call(server, "run_gaql", query="SELECT campaign.name FROM campaign WHERE campaign.id = 1",
                  customer_id="111-222-3333")
    assert result["customer_id"] == "111-222-3333"


def test_no_delete_or_remove_status(server):
    with pytest.raises(ToolError):
        call(server, "set_status", object_type="keyword", ids=["111~222"], status="REMOVED")


def test_no_tool_can_delete_or_touch_billing_or_users(server):
    async def names():
        async with Client(server) as client:
            return [t.name for t in await client.list_tools()]
    for name in asyncio.run(names()):
        assert not any(word in name for word in ("delete", "billing", "user", "account_setting"))
        # The only removing tool: it undoes negative keywords (explicitly allowed by the brief).
        assert "remove" not in name or name == "remove_negative_keywords"


def test_bad_id_format_is_refused(server):
    with pytest.raises(ToolError, match="adGroupId~criterionId"):
        call(server, "set_status", object_type="keyword", ids=["222"], status="PAUSED")


# ---- the individual tools ----------------------------------------------------

def test_add_keywords_skips_existing(server):
    result = call(server, "add_keywords", ad_group_id="111", keywords=[
        {"text": "espresso beans", "match_type": "PHRASE"},     # exists (case-insensitive)
        {"text": "organic  espresso   beans", "match_type": "EXACT"},
    ])
    assert result["mode"] == "dry_run"
    assert [c["object"] for c in result["changes"]] == [
        "keyword 'organic espresso beans' [EXACT] in ad group 'Espresso' (campaign 'Coffee DE - Search')"]
    assert any("already exists" in w for w in result["warnings"])


def test_device_adjustment_shows_percent(server):
    result = call(server, "set_device_bid_adjustment", campaign_id="555", device="MOBILE", adjustment_percent=-20)
    assert result["changes"][0]["old"] == "no adjustment (0%)"
    assert result["changes"][0]["new"] == "-20%"


def test_device_adjustment_out_of_range(server):
    with pytest.raises(ToolError, match="between -100"):
        call(server, "set_device_bid_adjustment", campaign_id="555", device="MOBILE", adjustment_percent=-150)


def test_schedule_updates_existing_window(server):
    result = call(server, "set_ad_schedule_bid_adjustment", campaign_id="555", days=["MONDAY"],
                  start="00:00", end="06:00", adjustment_percent=-50)
    assert result["changes"][0]["old"] == "-30%" and result["changes"][0]["new"] == "-50%"


def test_schedule_refuses_overlap(server):
    with pytest.raises(ToolError, match="overlaps"):
        call(server, "set_ad_schedule_bid_adjustment", campaign_id="555", days=["MONDAY"],
             start="05:00", end="09:00", adjustment_percent=10)


def test_schedule_new_window_on_free_day(server):
    result = call(server, "set_ad_schedule_bid_adjustment", campaign_id="555", days=["SUNDAY"],
                  start="08:00", end="12:30", adjustment_percent=15)
    assert result["changes"][0]["old"] == "no time window yet" and result["changes"][0]["new"] == "+15%"


def test_rsa_update_keeps_pins_and_shows_old_new(server):
    result = call(server, "update_responsive_search_ad", ad_id="999", final_url="https://example.com/new",
                  headlines=["Brand", "Espresso", "Fresh roast", "New headline"])
    changes = {c["field"]: c for c in result["changes"]}
    assert changes["final_url"]["old"] == ["https://example.com/old"]
    assert changes["headlines"]["new"][0] == "Brand [pinned HEADLINE_1]"
    assert "descriptions" not in changes


def test_rsa_headline_too_long(server):
    with pytest.raises(ToolError, match="limit is 30"):
        call(server, "update_responsive_search_ad", ad_id="999",
             headlines=["Brand", "Espresso", "This headline is far too long for Google"])


def test_percent_conversion():
    assert writes.percent_to_modifier(-20) == 0.8
    assert writes.modifier_to_text(0.7) == "-30%"
    assert writes.modifier_to_text(1.15) == "+15%"
    assert writes.modifier_to_text(0) == "-100% (excluded)"
    assert writes.modifier_to_text(None) == "no adjustment (0%)"


def test_object_in_removed_campaign_is_refused(server, account, monkeypatch):
    original = account.search
    def search(customer_id, query, **kw):
        rows, more = original(customer_id, query, **kw)
        return [{**r, "campaign.status": "REMOVED"} for r in rows], more
    monkeypatch.setattr(account, "search", search)
    with pytest.raises(ToolError, match="removed"):
        call(server, "set_status", **PAUSE)
    assert account.mutations == []


def test_negative_keyword_is_refused(server, account, monkeypatch):
    original = account.search
    def search(customer_id, query, **kw):
        rows, more = original(customer_id, query, **kw)
        return [{**r, "ad_group_criterion.negative": True} for r in rows], more
    monkeypatch.setattr(account, "search", search)
    with pytest.raises(ToolError, match="negative keyword"):
        call(server, "set_status", **PAUSE)
    assert account.mutations == []


# ---- campaign budget -------------------------------------------------------

def test_budget_dry_run_shows_old_and_new_in_currency(server, account):
    result = call(server, "set_campaign_budget", campaign_id="555", daily_amount=50)
    assert result["mode"] == "dry_run"
    assert result["changes"][0]["old"] == "40.00 EUR" and result["changes"][0]["new"] == "50.00 EUR"
    assert any("spend more" in w for w in result["warnings"])
    assert account.mutations == [(True, "CampaignBudgetService", 1)]


def test_budget_above_profile_cap_is_refused(server, account):
    with pytest.raises(ToolError, match="above this profile's max_daily_budget"):
        call(server, "set_campaign_budget", campaign_id="555", daily_amount=150)
    assert account.mutations == []


def test_budget_step_too_large_is_refused(server, account):
    with pytest.raises(ToolError, match="at most 50%"):
        call(server, "set_campaign_budget", campaign_id="555", daily_amount=90)
    with pytest.raises(ToolError, match="at most 50%"):
        call(server, "set_campaign_budget", campaign_id="555", daily_amount=10)
    assert account.mutations == []


def test_shared_budget_is_refused(server, account):
    account.budget["shared"] = True
    with pytest.raises(ToolError, match="shared budget"):
        call(server, "set_campaign_budget", campaign_id="555", daily_amount=45)


def test_budget_needs_a_cap_in_the_profile(account, tmp_path):
    profile = Profile("fjord", {"client_id": "x"}, default_customer_id=DEMO, allowed_customer_ids=frozenset({DEMO}))
    with pytest.raises(ToolError, match="Budget changes are off"):
        call(create_server(profile, account, log_folder=tmp_path), "set_campaign_budget", campaign_id="555", daily_amount=45)


def test_budget_zero_is_refused(server):
    with pytest.raises(ToolError, match="pause the campaign"):
        call(server, "set_campaign_budget", campaign_id="555", daily_amount=0)


def test_budget_change_is_logged(server, account, tmp_path):
    token = call(server, "set_campaign_budget", campaign_id="555", daily_amount=45)["confirm_token"]
    call(server, "set_campaign_budget", campaign_id="555", daily_amount=45, confirm=True, confirm_token=token)
    entry = json.loads((tmp_path / "changes.jsonl").read_text().splitlines()[-1])
    assert entry["field"] == "daily_budget" and entry["old"] == "40.00 EUR" and entry["new"] == "45.00 EUR"


def test_tokens_of_existing_tools_expire_too(server, account, monkeypatch):
    import time as _time
    from ads_pilot_mcp import writes as _writes
    token = call(server, "set_status", **PAUSE)["confirm_token"]
    real = _time.time
    monkeypatch.setattr(_writes.time, "time", lambda: real() + _writes.TOKEN_TTL_SECONDS + 5)
    with pytest.raises(ToolError, match="expired"):
        call(server, "set_status", **PAUSE, confirm=True, confirm_token=token)
    assert all(validate_only for validate_only, *_ in account.mutations)
