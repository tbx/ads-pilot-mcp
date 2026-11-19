"""Offline tests for location, language and network targeting. A fake account stands in for Google Ads."""

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
GEO = {"2620": ("Portugal", "Portugal", "Country"), "1011742": ("Lisbon", "Lisbon,Lisbon,Portugal", "City"),
       "2276": ("Germany", "Germany", "Country")}


class FakeAccount:
    def __init__(self):
        self._client = GoogleAdsClient(credentials=AnonymousCredentials(), version="v26", use_proto_plus=True)
        self.mutations = []
        self.channel = "SEARCH"
        self.locations = {"2620": False}  # geo id -> negative?
        self.languages = ["1001"]
        self.network = {"target_search_network": False, "target_content_network": False}

    def client(self, login_customer_id=None):
        return self._client

    def suggest_geo_targets(self, names, country_code=None, locale="en"):
        out = {}
        for n in names:
            if n.lower() == "springfield":
                out[n] = [{"id": "1", "name": "Springfield", "canonical_name": "Springfield,Illinois,United States",
                           "type": "City", "status": "ENABLED"},
                          {"id": "2", "name": "Springfield", "canonical_name": "Springfield,Missouri,United States",
                           "type": "City", "status": "ENABLED"}]
            else:
                out[n] = [{"id": i, "name": nm, "canonical_name": cn, "type": ty, "status": "ENABLED"}
                          for i, (nm, cn, ty) in GEO.items() if nm.lower() == n.lower()]
        return out

    def search(self, customer_id, query, login_customer_id=None, max_rows=200):
        if "FROM campaign WHERE campaign.id" in query:
            return [{"campaign.resource_name": f"customers/{customer_id}/campaigns/555", "campaign.name": "Coffee DE",
                     "campaign.status": "ENABLED", "campaign.advertising_channel_type": self.channel,
                     "campaign.geo_target_type_setting.positive_geo_target_type": "PRESENCE",
                     "campaign.network_settings.target_search_network": self.network["target_search_network"],
                     "campaign.network_settings.target_content_network": self.network["target_content_network"]}], False
        if "FROM geo_target_constant" in query:
            return [{"geo_target_constant.id": int(i), "geo_target_constant.name": n, "geo_target_constant.canonical_name": c,
                     "geo_target_constant.target_type": ty, "geo_target_constant.status": "ENABLED"}
                    for i, (n, c, ty) in GEO.items() if i in query], False
        if "campaign_criterion.type IN (LOCATION" in query:
            return [{"campaign_criterion.resource_name": f"customers/{customer_id}/campaignCriteria/555~{g}",
                     "campaign_criterion.type": "LOCATION", "campaign_criterion.negative": neg,
                     "campaign_criterion.location.geo_target_constant": f"geoTargetConstants/{g}"}
                    for g, neg in self.locations.items()], False
        if "FROM language_constant" in query:
            return [{"language_constant.id": 1001, "language_constant.code": "de", "language_constant.name": "German",
                     "language_constant.targetable": True},
                    {"language_constant.id": 1000, "language_constant.code": "en", "language_constant.name": "English",
                     "language_constant.targetable": True}], False
        if "campaign_criterion.type = LANGUAGE" in query:
            return [{"campaign_criterion.resource_name": f"customers/{customer_id}/campaignCriteria/555~{l}",
                     "campaign_criterion.language.language_constant": f"languageConstants/{l}"}
                    for l in self.languages], False
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


# ---- shared rules, for each new tool ---------------------------------------------

CASES = [("set_location_targeting", {"campaign_id": "555", "add": ["Lisbon"]}),
         ("set_language_targeting", {"campaign_id": "555", "add": ["en"]}),
         ("set_network_settings", {"campaign_id": "555", "search_partners": True})]


@pytest.mark.parametrize("tool,args", CASES)
def test_dry_run_changes_nothing(server, account, tmp_path, tool, args):
    result = call(server, tool, **args)
    assert result["mode"] == "dry_run" and result["confirm_token"]
    assert all(v for v, *_ in account.mutations) and not (tmp_path / "changes.jsonl").exists()


@pytest.mark.parametrize("tool,args", CASES)
def test_wrong_token_and_other_account_are_refused(server, account, tool, args):
    call(server, tool, **args)
    with pytest.raises(ToolError, match="confirm_token"):
        call(server, tool, **args, confirm=True, confirm_token="1.abc")
    with pytest.raises(ToolError, match="not in allowed_customer_ids"):
        call(server, tool, **args, customer_id="111-222-3333")
    assert all(v for v, *_ in account.mutations)


@pytest.mark.parametrize("tool,args", CASES)
def test_execute_logs_change(server, account, tmp_path, tool, args):
    token = call(server, tool, **args)["confirm_token"]
    result = call(server, tool, **args, confirm=True, confirm_token=token)
    assert result["mode"] == "executed"
    entry = json.loads((tmp_path / "changes.jsonl").read_text().splitlines()[-1])
    assert entry["tool"] == tool and entry["object_id"] == "555" and entry["change_id"]


# ---- locations ---------------------------------------------------------------

def test_location_name_is_resolved_and_shown(server, account):
    result = call(server, "set_location_targeting", campaign_id="555", add=["Lisbon"], exclude=["2276"])
    objects = [c["object"] for c in result["changes"]]
    assert "location 'Lisbon,Lisbon,Portugal' (City, 1011742) in campaign 'Coffee DE' (SEARCH)" in objects
    assert any(c["new"] == "excluded" and "Germany" in c["object"] for c in result["changes"])
    assert account.mutations[-1][1] == "GoogleAdsService"


def test_ambiguous_name_is_refused_with_candidates(server):
    with pytest.raises(ToolError, match="ambiguous.*Illinois.*Missouri"):
        call(server, "set_location_targeting", campaign_id="555", add=["Springfield"])


def test_removing_last_location_warns_worldwide(server):
    result = call(server, "set_location_targeting", campaign_id="555", remove=["2620"])
    assert result["warnings"][0].startswith("WARNING") and "WORLDWIDE" in result["warnings"][0]
    assert result["changes"][0]["old"] == "targeted" and result["changes"][0]["new"] == "removed"


def test_campaign_without_locations_gets_a_note(server, account):
    account.locations = {}
    result = call(server, "set_location_targeting", campaign_id="555", exclude=["2276"])
    assert "worldwide" in result["warnings"][0]


def test_add_existing_is_skipped_and_excluded_needs_removal_first(server, account):
    assert call(server, "set_location_targeting", campaign_id="555", add=["Portugal"])["mode"] == "no_change"
    account.locations = {"2620": True}
    with pytest.raises(ToolError, match="currently excluded"):
        call(server, "set_location_targeting", campaign_id="555", add=["2620"])


def test_location_type_change(server):
    result = call(server, "set_location_targeting", campaign_id="555", location_type="PRESENCE_OR_INTEREST")
    assert result["changes"][0]["field"] == "positive_geo_target_type"
    assert result["changes"][0]["old"].startswith("presence (") and "interest" in result["changes"][0]["new"]


def test_location_changes_can_be_locked(account, tmp_path):
    with pytest.raises(ToolError, match="allow_location_changes: false"):
        call(make_server(account, tmp_path, allow_location_changes=False), "set_location_targeting",
             campaign_id="555", add=["Lisbon"])
    assert account.mutations == []


# ---- languages ---------------------------------------------------------------

def test_language_by_code_and_name(server):
    result = call(server, "set_language_targeting", campaign_id="555", add=["en", "German"])
    assert [c["object"] for c in result["changes"]] == ["language English (en, 1000) in campaign 'Coffee DE' (SEARCH)"]
    assert any("already targeted" in w for w in result["warnings"])


def test_removing_last_language_warns(server):
    result = call(server, "set_language_targeting", campaign_id="555", remove=["de"])
    assert "ALL languages" in result["warnings"][0]


def test_unknown_language_is_refused(server):
    with pytest.raises(ToolError, match="Unknown language 'Klingon'"):
        call(server, "set_language_targeting", campaign_id="555", add=["Klingon"])


# ---- networks ----------------------------------------------------------------

def test_network_settings_old_and_new(server):
    result = call(server, "set_network_settings", campaign_id="555", search_partners=True, display_network=False)
    assert result["changes"] == [{"object": "Search partners in campaign 'Coffee DE' (SEARCH)",
                                  "field": "target_search_network", "old": "off", "new": "on"}]
    assert any("already off" in w for w in result["warnings"])


def test_network_settings_refused_for_pmax(server, account):
    account.channel = "PERFORMANCE_MAX"
    with pytest.raises(ToolError, match="Performance Max always uses all Google networks"):
        call(server, "set_network_settings", campaign_id="555", display_network=False)
    assert account.mutations == []
