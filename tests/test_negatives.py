"""Offline tests for negative keywords. A fake account stands in for Google Ads."""

import asyncio
import json
import time

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from ads_pilot_mcp import writes
from ads_pilot_mcp.ads import MutateResult
from ads_pilot_mcp.config import Profile
from ads_pilot_mcp.negatives import would_block
from ads_pilot_mcp.server import create_server

DEMO = "1234567890"


class FakeAccount:
    def __init__(self):
        self._client = GoogleAdsClient(credentials=AnonymousCredentials(), version="v26", use_proto_plus=True)
        self.mutations = []  # (validate_only, service, ops, partial_failure)
        self.campaign_negatives = {("cheap coffee", "PHRASE"): "customers/1/campaignCriteria/555~1"}
        self.list_negatives = {}
        self.positive = [("organic espresso beans", "PHRASE"), ("coffee subscription", "EXACT")]
        self.account_list = True
        self.fail_index = None  # simulate Google rejecting one operation
        self.linked = False

    def client(self, login_customer_id=None):
        return self._client

    def search(self, customer_id, query, login_customer_id=None, max_rows=200):
        if "FROM campaign WHERE campaign.id" in query:
            return [{"campaign.resource_name": f"customers/{customer_id}/campaigns/555", "campaign.name": "Coffee DE",
                     "campaign.status": "ENABLED", "campaign.advertising_channel_type": "PERFORMANCE_MAX"}], False
        if "FROM ad_group WHERE ad_group.id" in query:
            return [{"ad_group.resource_name": f"customers/{customer_id}/adGroups/111", "ad_group.name": "Espresso",
                     "ad_group.status": "ENABLED", "campaign.name": "Coffee DE", "campaign.status": "ENABLED"}], False
        if "FROM shared_set WHERE shared_set.id" in query:
            return [{"shared_set.resource_name": f"customers/{customer_id}/sharedSets/900", "shared_set.name": "Brand junk",
                     "shared_set.type": "NEGATIVE_KEYWORDS", "shared_set.status": "ENABLED",
                     "shared_set.member_count": 3}], False
        if "ACCOUNT_LEVEL_NEGATIVE_KEYWORDS" in query:
            return ([{"shared_set.resource_name": f"customers/{customer_id}/sharedSets/901", "shared_set.id": 901,
                      "shared_set.name": "Account negatives"}] if self.account_list else []), False
        if "FROM shared_set WHERE shared_set.type = NEGATIVE_KEYWORDS" in query:
            return [{"shared_set.name": "Brand junk"}], False
        if "FROM campaign_shared_set" in query and "campaign.id =" in query:
            return ([{"campaign_shared_set.resource_name": f"customers/{customer_id}/campaignSharedSets/555~900"}]
                    if self.linked else []), False
        if "FROM campaign_shared_set" in query:
            return [{"campaign.id": 555}], False
        if "FROM campaign_criterion" in query:
            return [{"campaign_criterion.resource_name": rn, "campaign_criterion.keyword.text": t,
                     "campaign_criterion.keyword.match_type": m} for (t, m), rn in self.campaign_negatives.items()], False
        if "FROM shared_criterion" in query:
            return [{"shared_criterion.resource_name": rn, "shared_criterion.keyword.text": t,
                     "shared_criterion.keyword.match_type": m} for (t, m), rn in self.list_negatives.items()], False
        if "FROM ad_group_criterion" in query and "negative = TRUE" in query:
            return [], False
        if "FROM ad_group_criterion" in query:
            return [{"ad_group_criterion.keyword.text": t, "ad_group_criterion.keyword.match_type": m,
                     "ad_group.name": "Espresso", "campaign.name": "Coffee DE"} for t, m in self.positive], False
        raise AssertionError(f"unexpected query: {query}")

    def mutate(self, customer_id, service, method, operations, login_customer_id=None, validate_only=True,
               partial_failure=False):
        self.mutations.append((validate_only, service, len(operations), partial_failure))
        failures = {self.fail_index: "criterion_error=KEYWORD_HAS_INVALID_CHARS | bad text"} \
            if self.fail_index is not None else {}
        return MutateResult(results=[f"r{i}" for i in range(len(operations)) if i not in failures], failures=failures)


@pytest.fixture
def account():
    return FakeAccount()


@pytest.fixture
def profile():
    return Profile("fjord", {"client_id": "x"}, default_customer_id=DEMO, allowed_customer_ids=frozenset({DEMO}),
                   max_negative_keywords_per_call=3)


@pytest.fixture
def server(account, profile, tmp_path):
    return create_server(profile, account, log_folder=tmp_path)


def call(server, tool, **args):
    async def run():
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return json.loads(result.content[0].text)
    return asyncio.run(run())


ADD = {"level": "campaign", "campaign_id": "555",
       "keywords": [{"text": "free coffee", "match_type": "PHRASE"}, {"text": "cheap coffee", "match_type": "PHRASE"}]}


# ---- add ---------------------------------------------------------------------

def test_dry_run_changes_nothing_and_skips_existing(server, account, tmp_path):
    result = call(server, "add_negative_keywords", **ADD)
    assert result["mode"] == "dry_run" and result["profile"] == "fjord"
    assert [c["object"] for c in result["changes"]] == [
        "negative keyword 'free coffee' [PHRASE] on campaign 'Coffee DE' (555, PERFORMANCE_MAX)"]
    assert any("Already negative" in w and "cheap coffee" in w for w in result["warnings"])
    assert result["confirm_token_valid_minutes"] == 15
    assert account.mutations == [(True, "CampaignCriterionService", 1, True)]
    assert not (tmp_path / "changes.jsonl").exists()


def test_execute_logs_change_id_and_fields(server, account, tmp_path):
    token = call(server, "add_negative_keywords", **ADD)["confirm_token"]
    result = call(server, "add_negative_keywords", **ADD, confirm=True, confirm_token=token)
    assert result["mode"] == "executed" and result["changes"][0]["change_id"]
    entry = json.loads((tmp_path / "changes.jsonl").read_text().splitlines()[0])
    assert entry["change_id"] == result["changes"][0]["change_id"]
    assert entry["tool"] == "add_negative_keywords" and entry["object_type"] == "campaign_negative_keyword"
    assert entry["object_id"] == "555" and entry["name"] == "free coffee [PHRASE]"
    assert entry["old"] is None and entry["new"] == "added" and entry["profile"] == "fjord"


def test_wrong_token_is_refused(server, account):
    call(server, "add_negative_keywords", **ADD)
    with pytest.raises(ToolError, match="confirm_token"):
        call(server, "add_negative_keywords", **ADD, confirm=True, confirm_token="123.abc")
    assert all(v for v, *_ in account.mutations)


def test_expired_token_is_refused(server, account, monkeypatch):
    token = call(server, "add_negative_keywords", **ADD)["confirm_token"]
    real = time.time
    monkeypatch.setattr(writes.time, "time", lambda: real() + writes.TOKEN_TTL_SECONDS + 5)
    with pytest.raises(ToolError, match="expired"):
        call(server, "add_negative_keywords", **ADD, confirm=True, confirm_token=token)
    assert all(v for v, *_ in account.mutations)


def test_account_outside_allowlist_is_refused(server, account):
    with pytest.raises(ToolError, match="not in allowed_customer_ids"):
        call(server, "add_negative_keywords", **ADD, customer_id="111-222-3333")
    assert account.mutations == []


def test_profile_limit_per_call(server, account):
    kws = [{"text": f"word {i}", "match_type": "EXACT"} for i in range(4)]
    with pytest.raises(ToolError, match="at most 3 per call"):
        call(server, "add_negative_keywords", level="campaign", campaign_id="555", keywords=kws)
    assert account.mutations == []


def test_conflict_with_own_keyword_is_warned(server):
    result = call(server, "add_negative_keywords", level="campaign", campaign_id="555",
                  keywords=[{"text": "espresso", "match_type": "BROAD"}])
    assert any("would block your active keyword 'organic espresso beans'" in w for w in result["warnings"])


def test_partial_failure_is_reported(server, account, tmp_path):
    args = {"level": "campaign", "campaign_id": "555",
            "keywords": [{"text": "free coffee", "match_type": "PHRASE"}, {"text": "bad!!", "match_type": "EXACT"}]}
    account.fail_index = 1
    dry = call(server, "add_negative_keywords", **args)
    assert "would_fail" in dry["changes"][1] and "would_fail" not in dry["changes"][0]
    result = call(server, "add_negative_keywords", **args, confirm=True, confirm_token=dry["confirm_token"])
    assert result["mode"] == "partially_executed"
    assert len(result["changes"]) == 1 and result["failed"][0]["object"].startswith("negative keyword 'bad!!'")
    assert len((tmp_path / "changes.jsonl").read_text().splitlines()) == 1  # only the good one is logged


def test_account_level_needs_existing_list(server, account):
    account.account_list = False
    with pytest.raises(ToolError, match="no account-level negative keyword list"):
        call(server, "add_negative_keywords", level="account", keywords=[{"text": "jobs", "match_type": "BROAD"}])


def test_shared_list_level(server, account):
    result = call(server, "add_negative_keywords", level="shared_list", shared_set_id="900",
                  keywords=[{"text": "jobs", "match_type": "BROAD"}])
    assert account.mutations[-1][1] == "SharedCriterionService"
    assert "used by 1 campaigns" in result["changes"][0]["object"]


def test_missing_id_and_brackets_are_refused(server):
    with pytest.raises(ToolError, match="needs campaign_id"):
        call(server, "add_negative_keywords", level="campaign", keywords=[{"text": "x", "match_type": "EXACT"}])
    with pytest.raises(ToolError, match="without brackets"):
        call(server, "add_negative_keywords", level="campaign", campaign_id="555",
             keywords=[{"text": "[free coffee]", "match_type": "EXACT"}])


# ---- remove ------------------------------------------------------------------

def test_remove_existing_and_name_missing(server, account):
    result = call(server, "remove_negative_keywords", level="campaign", campaign_id="555",
                  keywords=[{"text": "Cheap Coffee", "match_type": "PHRASE"}, {"text": "nope", "match_type": "EXACT"}])
    assert result["changes"][0]["old"] == "present" and result["changes"][0]["new"] == "removed"
    assert any("nothing to remove" in w and "nope" in w for w in result["warnings"])
    assert account.mutations[-1][:2] == (True, "CampaignCriterionService")


def test_remove_nothing_found_is_no_change(server):
    result = call(server, "remove_negative_keywords", level="campaign", campaign_id="555",
                  keywords=[{"text": "nope", "match_type": "EXACT"}])
    assert result["mode"] == "no_change"


# ---- lists -------------------------------------------------------------------

def test_list_create_and_duplicate_name(server, account):
    result = call(server, "manage_negative_keyword_list", action="create", name="Jobs and careers")
    assert result["changes"][0]["new"] == "created" and account.mutations[-1][1] == "SharedSetService"
    with pytest.raises(ToolError, match="already exists"):
        call(server, "manage_negative_keyword_list", action="create", name="brand junk")


def test_list_attach_and_detach(server, account):
    result = call(server, "manage_negative_keyword_list", action="attach", shared_set_id="900", campaign_id="555")
    assert result["changes"][0]["new"] == "attached"
    assert call(server, "manage_negative_keyword_list", action="detach", shared_set_id="900",
                campaign_id="555")["mode"] == "no_change"
    account.linked = True
    result = call(server, "manage_negative_keyword_list", action="detach", shared_set_id="900", campaign_id="555")
    assert result["changes"][0]["new"] == "detached" and account.mutations[-1][1] == "CampaignSharedSetService"


# ---- matching ----------------------------------------------------------------

@pytest.mark.parametrize("neg,match,pos,blocked", [
    ("espresso", "BROAD", "organic espresso beans", True),
    ("espresso beans", "PHRASE", "organic espresso beans", True),
    ("beans espresso", "PHRASE", "organic espresso beans", False),
    ("organic espresso beans", "EXACT", "+organic +espresso +beans", True),
    ("espresso", "EXACT", "organic espresso beans", False),
])
def test_would_block(neg, match, pos, blocked):
    assert would_block(neg, match, pos) is blocked
