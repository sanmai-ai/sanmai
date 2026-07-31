# Contributing to SanMai AI

Thanks for helping build SanMai AI. This guide covers local setup and the checks
your pull request must pass.

## Before you start

- Read [`LICENSE`](./LICENSE) — SanMai AI is source-available, not OSI open source.
- Opening a PR indicates acceptance of the [Contributor License Agreement](./CLA.md).
- Report security issues **privately** per [`SECURITY.md`](./SECURITY.md) — never
  in a public issue or PR.

## Dev setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
```

The core boots and its tests run entirely against fakes — no cloud credentials
are required. Copy [`.env.example`](./.env.example) to `.env` for local config.

## Before you open a PR

Run both and make sure they pass clean:

```bash
pytest        # all tests
ruff check .  # lint
```

Please also:

- Keep changes scoped and typed (this codebase uses type hints; `mypy` is available).
- Add or update tests for behavior you change.
- Follow the adapter contract: new providers implement the `base.py` port for
  their port and must stay swappable with the existing fakes.

## Commit / PR hygiene

- Small, focused commits with clear messages.
- Describe what changed and why in the PR body.
- CI runs `pytest` + `ruff`; green is required to merge.
