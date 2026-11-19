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
    allowed_customer_ids:      # accounts this server may change
      - "123-456-7890"
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

    def resolve_customer_id(self, customer_id: str | None) -> str:
        return normalize_customer_id(customer_id or self.default_customer_id)

    def resolve_login_customer_id(self, login_customer_id: str | None) -> str | None:
        value = login_customer_id or self.login_customer_id
        return normalize_customer_id(value) if value else None

    def is_write_allowed(self, customer_id: str) -> bool:
        return normalize_customer_id(customer_id) in self.allowed_customer_ids


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

    return Profile(
        name=name,
        auth=dict(auth),
        default_customer_id=optional_id("default_customer_id"),
        login_customer_id=optional_id("login_customer_id"),
        allowed_customer_ids=frozenset(normalize_customer_id(v) for v in data.get("allowed_customer_ids") or []),
        max_daily_budget=optional_number("max_daily_budget"),
        max_budget_change_percent=optional_number("max_budget_change_percent") or 50.0,
        max_negative_keywords_per_call=int(optional_number("max_negative_keywords_per_call") or 50),
    )
