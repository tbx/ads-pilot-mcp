# Contributing

Thanks for wanting to improve ads-pilot-mcp.

## Before you start

- Open an issue first for anything larger than a small fix, so we can agree on the approach.
- Keep it small and readable. Fewer tools that work safely beat many half-finished ones.
- The guardrails are not negotiable: dry run by default, allowlist per profile, change log, no delete, budgets only within the profile's limits, no billing, users or account settings.

## Contributor License Agreement

ads-pilot-mcp is offered under the AGPL-3.0 and, separately, under a commercial license. To keep that possible, every contribution needs a Contributor License Agreement (CLA): you keep the copyright of your contribution, and you grant the author the right to distribute it under both licenses. Please say in your pull request that you agree; the CLA text will be linked here.

## Development

```bash
uv sync
uv run pytest
```

Tests run offline and never touch a real Google Ads account. Code, comments and docs are in English. Please do not use em or en dashes in user-facing text.
