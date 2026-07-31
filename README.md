# SanMai AI

**The AI-native, self-hostable restaurant OS — core engine.**

SanMai AI is a source-available restaurant operating system: ordering, payments,
fiscal receipts/invoices, staff, inventory, and an AI layer — designed to run as
your own single-tenant deployment. The core is provider-agnostic: payments, LLM,
fiscal rules, notifications, storage, and identity are all pluggable **adapters**
behind stable ports, so the engine boots and is fully testable with **fakes and
zero external credentials**.

> **License:** source-available, **not** OSI open source. See [`LICENSE`](./LICENSE).

## Layout

- `be/` — FastAPI backend (async SQLAlchemy, raw SQL via `text()`), adapters, config.
- `be/adapters/` — provider ports (`base.py`) + fakes (`demo`/`echo`/`generic`/…).
- `be/config.py` — single Pydantic `BaseSettings`; env-only, fail-loud at boot.
- `cli/` — the `sanmai` CLI (init/provision/deploy/doctor/…).

## Develop

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
pytest
```

Everything runs against in-memory / tempdir fakes — no cloud credentials needed.

### Config

All settings are read from the environment with the `SANMAI_` prefix (see
[`.env.example`](./.env.example)). Config is **fail-loud**: a missing or
mistyped required key raises at boot, not mid-request. Copy the example and
export it, or use your own secrets tooling:

```bash
cp .env.example .env
```

## Contributing & security

- Read [`CONTRIBUTING.md`](./CONTRIBUTING.md) — run `pytest` and `ruff` before every PR.
- Contributors sign the [`CLA`](./CLA.md).
- Report vulnerabilities privately per [`SECURITY.md`](./SECURITY.md) — do **not** open a public issue.
