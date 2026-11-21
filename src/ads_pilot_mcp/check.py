# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Self check that changes nothing.

1. loads the profile, 2. signs in, 3. reads the default account,
4. lets Google validate a write (pausing one ad group) with validate_only,
   which proves write access without changing anything.

Run with:  ADS_PILOT_PROFILE=fjord uv run ads-pilot-check
"""

from __future__ import annotations

import sys

from .ads import Ads, AdsError
from .config import ConfigError, format_customer_id, load_profile
from .writes import WriteRefused, plan_set_status


def main() -> None:
    try:
        profile = load_profile()
    except ConfigError as e:
        sys.exit(f"ERROR profile: {e}")

    print(f"Profile:           {profile.name}")
    print(f"Default account:   {format_customer_id(profile.default_customer_id) if profile.default_customer_id else '-'}")
    print(f"Manager (login):   {format_customer_id(profile.login_customer_id) if profile.login_customer_id else '-'}")
    levels = profile.writable_accounts()
    print(f"May change:        {', '.join(f'{c} ({lv})' for c, lv in levels.items()) or '- (all accounts read_only)'}")

    ads = Ads(profile)
    try:
        ids = ads.list_accessible_customer_ids()
        print(f"OK sign-in:        {len(ids)} directly accessible account(s): {', '.join(format_customer_id(i) for i in ids)}")
    except AdsError as e:
        sys.exit(f"ERROR sign-in: {e}")

    if profile.default_customer_id:
        cid = profile.default_customer_id
        try:
            rows, _ = ads.search(
                cid,
                "SELECT customer.id, customer.descriptive_name FROM customer LIMIT 1",
                login_customer_id=profile.login_customer_id,
                max_rows=1,
            )
            name = rows[0]["customer.descriptive_name"] if rows else "?"
            print(f"OK read access:    {format_customer_id(cid)} = {name}")
        except AdsError as e:
            sys.exit(f"ERROR read access to {format_customer_id(cid)}: {e}")

        if profile.is_write_allowed(cid):
            check_write_access(ads, cid, profile.login_customer_id)
        else:
            print(f"SKIP write check:  {format_customer_id(cid)} is read_only in this profile")

    print("All good. Nothing was changed.")


def check_write_access(ads: Ads, cid: str, login: str | None) -> None:
    """Validate (not execute) pausing one enabled ad group."""
    try:
        rows, _ = ads.search(
            cid,
            "SELECT ad_group.id, ad_group.name FROM ad_group WHERE ad_group.status = 'ENABLED' "
            "AND campaign.status = 'ENABLED' LIMIT 1",
            login_customer_id=login,
            max_rows=1,
        )
        if not rows:
            print("SKIP write check:  no enabled ad group to test with")
            return
        plan = plan_set_status(ads, cid, login, "ad_group", [str(rows[0]["ad_group.id"])], "PAUSED")
        ads.mutate(cid, plan.service, plan.method, plan.operations, login, validate_only=True)
        print(f"OK write access:   dry run 'pause ad group {rows[0]['ad_group.name']}' accepted by Google (not executed)")
    except (AdsError, WriteRefused) as e:
        sys.exit(f"ERROR write access (dry run) on {format_customer_id(cid)}: {e}")


if __name__ == "__main__":
    main()
