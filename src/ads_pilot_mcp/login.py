# Copyright (C) 2026 Tobias Leingruber
# SPDX-License-Identifier: AGPL-3.0-only
"""Sign in once and store the OAuth values in a profile.

    uv run ads-pilot-login <profile name> [path to client_secret JSON]

Without a path, the newest client_secret_*.json in ~/Downloads is used.

The JSON file is the OAuth client (type "Desktop app") from the Google Cloud Console.
A browser window opens; sign in with the Google account that has access to the
Google Ads accounts. client_id, client_secret and refresh_token are then written
into profiles/<profile name>.yaml. Nothing is sent anywhere except to Google.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .config import ConfigError, load_profile, profiles_dir

SCOPES = ["https://www.googleapis.com/auth/adwords"]
KEYS = ("client_id", "client_secret", "refresh_token")


def write_auth_values(path: Path, values: dict[str, str]) -> None:
    """Replace the client_id / client_secret / refresh_token values, keep everything else
    (including the short notes after them)."""
    text = path.read_text(encoding="utf-8")
    for key in KEYS:
        pattern = rf"""^([ \t]+{key}:[ \t]*)("[^"\n]*"|'[^'\n]*'|[^#\n]*?)([ \t]*#.*)?$"""
        # json.dumps gives a safely quoted YAML string
        value = json.dumps(values[key])
        text, count = re.subn(pattern, lambda m: m.group(1) + value + (m.group(3) or ""), text, count=1, flags=re.M)
        if count == 0:
            raise ConfigError(f"Line '{key}:' under 'auth:' not found in {path.name}. Start from profiles/example.yaml.")
    path.write_text(text, encoding="utf-8")


def find_client_secret(folder: Path) -> Path | None:
    """Newest client_secret_*.json in `folder`, or None."""
    files = sorted(folder.glob("client_secret_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def main() -> None:
    if len(sys.argv) not in (2, 3):
        sys.exit(
            "Usage:    uv run ads-pilot-login <profile name> [path to client_secret JSON]\n"
            "Example:  uv run ads-pilot-login acme\n"
            "          (uses the newest client_secret_*.json in your Downloads folder)"
        )
    name = sys.argv[1]
    if len(sys.argv) == 3:
        secret_file = Path(sys.argv[2]).expanduser()
    else:
        downloads = Path.home() / "Downloads"
        found = find_client_secret(downloads)
        if not found:
            sys.exit(f"No client_secret_*.json found in {downloads}. Download it first (step 2e) or give the path.")
        secret_file = found
        print(f"Using {secret_file}")

    profile_path = profiles_dir() / f"{name}.yaml"
    if not profile_path.is_file():
        sys.exit(f"Profile not found: {profile_path}\nCopy profiles/example.yaml to profiles/{name}.yaml first (docs/setup.md, step 3).")
    if not secret_file.is_file():
        sys.exit(f"JSON file not found: {secret_file}")

    from google_auth_oauthlib.flow import InstalledAppFlow

    print("A browser window opens. Sign in with the Google account that has access to Google Ads.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), scopes=SCOPES)
    credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    if not credentials.refresh_token:
        sys.exit("Google did not return a refresh token. Please run the command again.")

    write_auth_values(
        profile_path,
        {
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
        },
    )
    try:
        load_profile(name)
    except ConfigError as e:
        sys.exit(f"Saved, but the profile has a problem: {e}")

    print(f"\nSigned in. The values are saved in {profile_path}.")
    print(f"Next step:  ADS_PILOT_PROFILE={name} uv run ads-pilot-check")


if __name__ == "__main__":
    main()
