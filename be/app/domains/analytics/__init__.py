"""Analytics (sales pipeline) domain — one-DB, venue-aware source-of-truth.

Ingests POS/export sales into ``analytics_sales_raw`` + ``analytics_sales`` (a generic
CSV/xlsx importer; no vendor-specific column quirks), serves the dashboard read
aggregations, and exposes a guarded text-to-SQL chat + LLM-assisted menu-sync /
suggestions — all admin-gated (``require_admin('analytics')``), which fixes the live
email-spoof holes. Vendor touch points go through the LLM and Storage seams only.

Layering mirrors the other domains: ``schemas`` (pydantic), ``crud`` (``text()`` SQL +
async fns), ``ingest`` (generic parser + upsert), ``service`` (LLM-backed flows,
read-only text-to-SQL), and ``router`` (thin endpoints).
"""

from be.app.domains.analytics.router import router

__all__ = ["router"]
