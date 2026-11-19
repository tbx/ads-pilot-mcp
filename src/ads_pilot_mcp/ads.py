# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Thin wrapper around the google-ads library: clients, queries, errors."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import proto
from google.auth.exceptions import GoogleAuthError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from .config import Profile

API_VERSION = "v26"

# The library logs every failed request on its own. We report errors ourselves.
logging.getLogger("google.ads.googleads.client").setLevel(logging.CRITICAL)


class AdsError(Exception):
    """A Google Ads API error, reduced to what a human needs to read."""


@dataclass
class MutateResult:
    """Resource names of the operations that worked, and the error per failed operation index."""

    results: list[str] = field(default_factory=list)
    failures: dict[int, str] = field(default_factory=dict)


# Short explanations in plain words, matched against the error code name.
HINTS = (
    ("DUPLICATE", "It already exists."),
    ("CAMPAIGN_TYPE", "Google does not allow this for this campaign type."),
    ("NOT_SUPPORTED", "Google does not support this for this object or campaign type."),
    ("NOT_ALLOWED", "Google does not allow this here."),
    ("POLICY", "It violates a Google Ads policy."),
    ("INVALID_CHARACTERS", "The text contains characters Google does not accept."),
    ("TOO_LONG", "The text is too long."),
    ("TOO_MANY", "A Google limit on the number of items is reached."),
    ("REMOVED", "The object or its parent is removed."),
    ("PERMISSION", "The signed-in Google account lacks the rights for this."),
    ("NOT_FOUND", "The object does not exist (any more)."),
)


def explain(code: str) -> str:
    return next((hint for key, hint in HINTS if key in code), "")


class Ads:
    """Holds one GoogleAdsClient per login_customer_id for the active profile."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self._clients: dict[str | None, GoogleAdsClient] = {}

    def client(self, login_customer_id: str | None = None) -> GoogleAdsClient:
        if login_customer_id not in self._clients:
            config = {**self.profile.auth, "use_proto_plus": True}
            if login_customer_id:
                config["login_customer_id"] = login_customer_id
            try:
                self._clients[login_customer_id] = GoogleAdsClient.load_from_dict(config, version=API_VERSION)
            except ValueError as e:
                raise AdsError(f"Invalid profile configuration: {e}") from e
        return self._clients[login_customer_id]

    # ---- reading -----------------------------------------------------------

    def list_accessible_customer_ids(self) -> list[str]:
        """IDs the logged-in user (or service account) can access directly."""
        with ads_errors(self.profile.name):
            service = self.client(None).get_service("CustomerService")
            response = service.list_accessible_customers()
        return [name.split("/")[-1] for name in response.resource_names]

    def search(
        self,
        customer_id: str,
        query: str,
        login_customer_id: str | None = None,
        max_rows: int = 200,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run a GAQL query. Returns (rows, truncated)."""
        rows: list[dict[str, Any]] = []
        with ads_errors(self.profile.name):
            service = self.client(login_customer_id).get_service("GoogleAdsService")
            for batch in service.search_stream(customer_id=customer_id, query=query):
                paths = list(batch.field_mask.paths)
                for row in batch.results:
                    if len(rows) >= max_rows:
                        return rows, True
                    rows.append({path: _value_at(row, path) for path in paths})
        return rows, False

    # ---- writing -----------------------------------------------------------

    def mutate(
        self,
        customer_id: str,
        service: str,
        method: str,
        operations: list,
        login_customer_id: str | None = None,
        validate_only: bool = True,
        partial_failure: bool = False,
    ) -> MutateResult:
        """Send operations in one request. validate_only=True changes nothing.

        Without partial_failure the request is atomic (all or nothing). With it, good
        operations go through and the failures are reported per operation index.
        """
        with ads_errors(self.profile.name):
            client = self.client(login_customer_id)
            svc = client.get_service(service)
            request = {"customer_id": customer_id, "operations": operations, "validate_only": validate_only}
            if partial_failure:
                request["partial_failure"] = True
            response = getattr(svc, method)(request=request)
        failures = _partial_failures(client, response) if partial_failure else {}
        results = [r.resource_name for i, r in enumerate(response.results) if i not in failures]
        return MutateResult(results=results, failures=failures)


class ads_errors:
    """Context manager: turn API and login errors into a short, readable AdsError."""

    def __init__(self, profile_name: str = "<profile>"):
        self.profile_name = profile_name

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if isinstance(exc, GoogleAdsException):
            messages = []
            for error in exc.failure.errors:
                messages.append(_describe_error(error))
            raise AdsError(f"Google Ads API error (request_id {exc.request_id}): " + "; ".join(messages)) from exc
        if isinstance(exc, GoogleAuthError):
            if "invalid_grant" in str(exc):
                raise AdsError(
                    "The Google sign-in has expired or was revoked. Sign in again with: "
                    f"uv run ads-pilot-login {self.profile_name} <path to client_secret JSON>"
                ) from exc
            raise AdsError(f"Google sign-in failed, check the credentials in the profile: {exc}") from exc
        return False


def _describe_error(error) -> str:
    """'criterion_error=KEYWORD_HAS_INVALID_CHARS | message | field: ... | plain-word hint'."""
    code = next((f"{k}={v}" for k, v in _error_code(error).items()), "")
    field = ".".join(e.field_name for e in error.location.field_path_elements)
    hint = explain(code)
    return " | ".join(p for p in (code, error.message, field and f"field: {field}", hint) if p)


def _partial_failures(client, response) -> dict[int, str]:
    """Map operation index -> readable error from a partial_failure response."""
    status = response.partial_failure_error
    if not status or not getattr(status, "code", 0):
        return {}
    failure_cls = type(client.get_type("GoogleAdsFailure"))
    failures: dict[int, str] = {}
    for detail in status.details:
        failure = failure_cls.deserialize(detail.value)
        for error in failure.errors:
            index = next((e.index for e in error.location.field_path_elements if e.field_name == "operations"), -1)
            message = _describe_error(error)
            failures[index] = f"{failures[index]}; {message}" if index in failures else message
    return failures


def _error_code(error) -> dict[str, str]:
    """Return the one set error code, e.g. {'authorization_error': 'USER_PERMISSION_DENIED'}."""
    code = error.error_code
    which = type(code).pb(code).WhichOneof("error_code")
    if not which:
        return {}
    value = getattr(code, which)
    return {which: getattr(value, "name", str(value))}


def _value_at(row: Any, path: str) -> Any:
    """Read 'campaign.name' from a GoogleAdsRow and make it JSON friendly.

    Optional fields that are not set come back as None, not as 0. Otherwise an
    unset bid adjustment would look like -100%.
    """
    value = row
    for part in path.split("."):
        parent = value
        value = getattr(parent, part)
    if isinstance(parent, proto.Message):
        pb = type(parent).pb(parent)
        field = pb.DESCRIPTOR.fields_by_name.get(part)
        if field is not None and field.has_presence and field.message_type is None and not pb.HasField(part):
            return None
    return to_plain(value)


def to_plain(value: Any) -> Any:
    if isinstance(value, proto.Enum):
        return value.name
    if isinstance(value, proto.Message):
        return type(value).to_dict(value, use_integers_for_enums=False, preserving_proto_field_name=True)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__iter__"):
        return [to_plain(v) for v in value]
    return str(value)
