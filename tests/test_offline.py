"""Offline tests: no network, no credentials, no changes to any Google Ads account."""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from google.ads.googleads import config as ads_config
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials
from google.ads.googleads.errors import GoogleAdsException

from ads_pilot_mcp.ads import ads_errors, AdsError, _value_at
from ads_pilot_mcp.config import ConfigError, load_profile, normalize_customer_id
from ads_pilot_mcp.server import create_server

PROFILE_YAML = """
auth:
  client_id: x
  client_secret: y
  refresh_token: z
default_customer_id: "123-456-7890"
allowed_customer_ids: ["123-456-7890"]
"""


@pytest.fixture
def profile(tmp_path, monkeypatch):
    (tmp_path / "test.yaml").write_text(PROFILE_YAML)
    monkeypatch.setenv("ADS_PILOT_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv("ADS_PILOT_PROFILE", "test")
    return load_profile()


class FakeAds:
    """Stands in for the real API. Records calls, returns canned rows."""

    def __init__(self):
        self.calls = []

    def list_accessible_customer_ids(self):
        return ["1234567890"]

    def search(self, customer_id, query, login_customer_id=None, max_rows=200):
        self.calls.append((customer_id, login_customer_id, query))
        return [{"customer.id": int(customer_id), "customer.descriptive_name": "Fjord Coffee Roasters",
                 "customer.manager": False}], False


def call(server, tool, **args):
    async def run():
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return json.loads(result.content[0].text)
    return asyncio.run(run())


# ---- profile ---------------------------------------------------------------

def test_customer_id_formats():
    assert normalize_customer_id("123-456-7890") == "1234567890"
    assert normalize_customer_id(1234567890) == "1234567890"
    with pytest.raises(ConfigError):
        normalize_customer_id("123-456")


def test_profile_loads(profile):
    assert profile.name == "test"
    assert profile.default_customer_id == "1234567890"
    assert profile.login_customer_id is None
    assert profile.is_write_allowed("123-456-7890")
    assert not profile.is_write_allowed("111-111-1111")


def test_missing_profile_env(monkeypatch):
    monkeypatch.delenv("ADS_PILOT_PROFILE", raising=False)
    with pytest.raises(ConfigError, match="ADS_PILOT_PROFILE"):
        load_profile()


def test_profile_name_cannot_escape_folder(monkeypatch):
    monkeypatch.setenv("ADS_PILOT_PROFILE", "../secrets")
    with pytest.raises(ConfigError, match="Invalid profile name"):
        load_profile()


def test_example_profile_is_valid(monkeypatch):
    from ads_pilot_mcp.config import PROJECT_DIR
    monkeypatch.setenv("ADS_PILOT_PROFILES_DIR", str(PROJECT_DIR / "profiles"))
    assert load_profile("example").default_customer_id == "1234567890"


def test_google_ads_library_accepts_profile(profile):
    """The library accepts our config without a developer token (no network call)."""
    parsed = ads_config.load_from_dict({**profile.auth, "use_proto_plus": True})
    assert "developer_token" not in parsed


def offline_client():
    """A real GoogleAdsClient that never talks to Google (for building messages)."""
    return GoogleAdsClient(credentials=AnonymousCredentials(), version="v26", use_proto_plus=True)


# ---- API helpers -----------------------------------------------------------

def test_row_values_are_plain():
    client = offline_client()
    row = client.get_type("GoogleAdsRow")
    row.campaign.name = "Brand"
    row.campaign.status = client.enums.CampaignStatusEnum.PAUSED
    row.metrics.clicks = 0
    assert _value_at(row, "campaign.name") == "Brand"
    assert _value_at(row, "campaign.status") == "PAUSED"
    assert _value_at(row, "metrics.clicks") == 0


def test_api_errors_become_readable():
    client = offline_client()
    failure = client.get_type("GoogleAdsFailure")
    error = client.get_type("GoogleAdsError")
    error.error_code.authorization_error = client.get_type("AuthorizationErrorEnum").AuthorizationError.USER_PERMISSION_DENIED
    error.message = "No access"
    failure.errors.append(error)
    with pytest.raises(AdsError, match="abc123.*authorization_error=USER_PERMISSION_DENIED.*No access"):
        with ads_errors():
            raise GoogleAdsException(None, None, failure, "abc123")


# ---- MCP tools -------------------------------------------------------------

def test_tools_are_registered(profile):
    async def names():
        async with Client(create_server(profile, FakeAds())) as client:
            return {t.name for t in await client.list_tools()}
    assert asyncio.run(names()) == {
        "show_profile", "list_accounts", "run_gaql", "recent_changes",
        "set_status", "add_keywords", "set_device_bid_adjustment",
        "set_ad_schedule_bid_adjustment", "update_responsive_search_ad", "set_campaign_budget",
        "add_negative_keywords", "remove_negative_keywords", "manage_negative_keyword_list",
        "set_location_targeting", "set_language_targeting", "set_network_settings",
    }


def test_run_gaql_uses_default_account_and_names_it(profile):
    fake = FakeAds()
    result = call(create_server(profile, fake), "run_gaql", query="SELECT customer.id FROM customer")
    assert result["profile"] == "test"
    assert result["customer_id"] == "123-456-7890"
    assert result["rows"][0]["customer.descriptive_name"] == "Fjord Coffee Roasters"
    assert fake.calls[0][0] == "1234567890"


def test_run_gaql_with_explicit_account_and_mcc(profile):
    fake = FakeAds()
    result = call(create_server(profile, fake), "run_gaql", query="SELECT customer.id FROM customer",
                  customer_id="111-222-3333", login_customer_id="999-888-7777")
    assert result["customer_id"] == "111-222-3333"
    assert result["login_customer_id"] == "999-888-7777"
    assert fake.calls[0][:2] == ("1112223333", "9998887777")


def test_bad_customer_id_is_rejected(profile):
    with pytest.raises(ToolError, match="profile test"):
        call(create_server(profile, FakeAds()), "run_gaql", query="SELECT customer.id FROM customer",
             customer_id="12345")


def test_list_accounts_marks_writable(profile):
    result = call(create_server(profile, FakeAds()), "list_accounts")
    assert result["profile"] == "test"
    account = result["accounts"][0]
    assert account["customer_id"] == "123-456-7890"
    assert account["name"] == "Fjord Coffee Roasters"
    assert account["writable"] is True


# ---- sign-in helper --------------------------------------------------------

def test_login_writes_values_into_profile(tmp_path):
    from ads_pilot_mcp.config import PROJECT_DIR
    from ads_pilot_mcp.login import write_auth_values

    path = tmp_path / "acme.yaml"
    path.write_text((PROJECT_DIR / "profiles" / "example.yaml").read_text())
    write_auth_values(path, {"client_id": "id.apps", "client_secret": "s3cr:et", "refresh_token": "1//tok\"en"})

    text = path.read_text()
    assert "docs/setup.md" in text  # the header note survives
    assert "never share this value" in text  # so does the note after the value
    import yaml
    auth = yaml.safe_load(text)["auth"]
    assert auth == {"client_id": "id.apps", "client_secret": "s3cr:et", "refresh_token": "1//tok\"en"}


def test_expired_sign_in_tells_what_to_do():
    from google.auth.exceptions import RefreshError
    with pytest.raises(AdsError, match="uv run ads-pilot-login fjord"):
        with ads_errors("fjord"):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")


def test_login_finds_newest_client_secret(tmp_path):
    import os
    from ads_pilot_mcp.login import find_client_secret

    assert find_client_secret(tmp_path) is None
    old, new = tmp_path / "client_secret_old.json", tmp_path / "client_secret_new.json"
    old.write_text("{}"); new.write_text("{}")
    os.utime(old, (1, 1))
    (tmp_path / "other.json").write_text("{}")
    assert find_client_secret(tmp_path) == new
