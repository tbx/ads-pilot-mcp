# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Profiles: one YAML file per Google Cloud login, chosen at start via env var.

A profile file looks like this (see profiles/example.yaml):

    auth:                      # passed unchanged to the google-ads library
      client_id: ...
      client_secret: ...
      refresh_token: ...
      # or instead: json_key_file_path: /path/to/service-account.json
    login_customer_id: ""      # optional, manager account (MCC) ID
    default_customer_id: "123-456-7890"
    accounts:                  # access level per account: read_only, standard or full
      "123-456-7890": standard
    max_daily_budget: 100      # optional, highest daily budget per campaign
    max_budget_change_percent: 50   # optional, largest change per step
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_DIR = Path(__file__).resolve().parents[2]

ENV_PROFILE = "ADS_PILOT_PROFILE"
ENV_PROFILES_DIR = "ADS_PILOT_PROFILES_DIR"
ENV_LOG_DIR = "ADS_PILOT_LOG_DIR"

AUTH_KEYS = {
    "client_id",
    "client_secret",
    "refresh_token",
    "json_key_file_path",
    "impersonated_email",
    "use_application_default_credentials",
}


# Access levels, modelled on Google Ads' own (read only, standard, admin):
# read_only: reading and reports only. standard: the daily work (status, keywords, negatives, ad texts,
# Performance Max texts, bids, budgets, targeting). full: also bid strategies, portfolios and conversion actions.
LEVELS = ("read_only", "standard", "full")


class ConfigError(Exception):
    """Raised when the profile is missing or invalid."""


def normalize_customer_id(value: str | int | None) -> str:
    """Turn '123-456-7890' or 1234567890 into '1234567890'. Raises on bad input."""
    if value is None or str(value).strip() == "":
        raise ConfigError("No customer ID given and no default_customer_id in the profile.")
    digits = re.sub(r"[\s-]", "", str(value))
    if not re.fullmatch(r"\d{10}", digits):
        raise ConfigError(f"Invalid customer ID '{value}'. Expected 10 digits, e.g. 123-456-7890.")
    return digits


def format_customer_id(customer_id: str) -> str:
    """'1234567890' -> '123-456-7890' (the way Google Ads shows it)."""
    return f"{customer_id[:3]}-{customer_id[3:6]}-{customer_id[6:]}"


@dataclass(frozen=True)
class Profile:
    name: str
    auth: dict
    default_customer_id: str | None = None
    login_customer_id: str | None = None
    allowed_customer_ids: frozenset[str] = field(default_factory=frozenset)
    # Budget guardrails: without max_daily_budget, budget changes are refused.
    max_daily_budget: float | None = None
    max_budget_change_percent: float = 50.0
    max_negative_keywords_per_call: int = 50
    allow_campaign_status_change: bool = True
    allow_location_changes: bool = True
    max_target_change_percent: float = 30.0
    max_target_cpa: float | None = None
    max_cpc: float | None = None
    # Access level per account (normalized ID -> level). Accounts in allowed_customer_ids (older profiles) are full.
    account_levels: dict[str, str] = field(default_factory=dict)
    allowed_bidding_strategies: frozenset[str] | None = None  # None = all supported strategies
    conversion_remove_lookback_days: int = 90
    max_conversion_actions_per_call: int = 50

    def resolve_customer_id(self, customer_id: str | None) -> str:
        return normalize_customer_id(customer_id or self.default_customer_id)

    def resolve_login_customer_id(self, login_customer_id: str | None) -> str | None:
        value = login_customer_id or self.login_customer_id
        return normalize_customer_id(value) if value else None

    def level_for(self, customer_id: str) -> str:
        cid = normalize_customer_id(customer_id)
        if cid in self.account_levels:
            return self.account_levels[cid]
        return "full" if cid in self.allowed_customer_ids else "read_only"

    def is_write_allowed(self, customer_id: str) -> bool:
        return self.level_for(customer_id) != "read_only"

    def writable_accounts(self) -> dict[str, str]:
        """Formatted account ID -> level, for every account that is not read_only."""
        ids = set(self.allowed_customer_ids) | set(self.account_levels)
        return {format_customer_id(c): self.level_for(c) for c in sorted(ids) if self.level_for(c) != "read_only"}


def profiles_dir() -> Path:
    return Path(os.environ.get(ENV_PROFILES_DIR) or PROJECT_DIR / "profiles")


def log_dir() -> Path:
    return Path(os.environ.get(ENV_LOG_DIR) or PROJECT_DIR / "logs")


def load_profile(name: str | None = None) -> Profile:
    """Load the profile named by `name` or by the ADS_PILOT_PROFILE env var."""
    name = name or os.environ.get(ENV_PROFILE)
    if not name:
        raise ConfigError(f"Environment variable {ENV_PROFILE} is not set (e.g. {ENV_PROFILE}=fjord).")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ConfigError(f"Invalid profile name '{name}'.")

    path = profiles_dir() / f"{name}.yaml"
    if not path.is_file():
        raise ConfigError(f"Profile file not found: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    auth = data.get("auth") or {}
    unknown = set(auth) - AUTH_KEYS
    if unknown:
        raise ConfigError(f"Unknown keys under 'auth' in {path.name}: {', '.join(sorted(unknown))}")
    if not auth:
        raise ConfigError(f"Section 'auth' is missing or empty in {path.name}.")

    def optional_id(key: str) -> str | None:
        value = data.get(key)
        return normalize_customer_id(value) if value not in (None, "") else None

    def optional_number(key: str) -> float | None:
        value = data.get(key)
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} in {path.name} must be a number, got '{value}'.")
        if number <= 0:
            raise ConfigError(f"{key} in {path.name} must be greater than 0.")
        return number

    def optional_bool(key: str, default: bool) -> bool:
        value = data.get(key)
        if value is None or value == "":
            return default
        if not isinstance(value, bool):
            raise ConfigError(f"{key} in {path.name} must be true or false, got '{value}'.")
        return value

    levels: dict[str, str] = {}
    accounts = data.get("accounts") or {}
    if not isinstance(accounts, dict):
        raise ConfigError(f"accounts in {path.name} must map account IDs to a level, e.g. \"123-456-7890\": standard.")
    for key, level in accounts.items():
        level = str(level or "").strip().lower()
        if level not in LEVELS:
            raise ConfigError(f"Level of account {key} in {path.name} must be one of {', '.join(LEVELS)}, got '{level}'.")
        levels[normalize_customer_id(key)] = level

    strategies = data.get("allowed_bidding_strategies")
    if strategies not in (None, "") and not isinstance(strategies, list):
        raise ConfigError(f"allowed_bidding_strategies in {path.name} must be a list, e.g. [MAXIMIZE_CONVERSIONS].")

    return Profile(
        name=name,
        auth=dict(auth),
        default_customer_id=optional_id("default_customer_id"),
        login_customer_id=optional_id("login_customer_id"),
        allowed_customer_ids=frozenset(normalize_customer_id(v) for v in data.get("allowed_customer_ids") or []),
        max_daily_budget=optional_number("max_daily_budget"),
        max_budget_change_percent=optional_number("max_budget_change_percent") or 50.0,
        max_negative_keywords_per_call=int(optional_number("max_negative_keywords_per_call") or 50),
        allow_campaign_status_change=optional_bool("allow_campaign_status_change", True),
        allow_location_changes=optional_bool("allow_location_changes", True),
        max_target_change_percent=optional_number("max_target_change_percent") or 30.0,
        max_target_cpa=optional_number("max_target_cpa"),
        max_cpc=optional_number("max_cpc"),
        account_levels=levels,
        allowed_bidding_strategies=frozenset(str(s).strip().upper() for s in strategies) if strategies else None,
        conversion_remove_lookback_days=int(optional_number("conversion_remove_lookback_days") or 90),
        max_conversion_actions_per_call=int(optional_number("max_conversion_actions_per_call") or 50),
    )
