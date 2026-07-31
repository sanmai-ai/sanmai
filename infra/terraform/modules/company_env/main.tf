# company_env — parameterized building block for one company x env.
# Skeleton only: enables the baseline APIs and provisions the runtime service
# account. Higher-level composition (cloud_run/sql/firestore/backup_vault) is
# wired by the calling env, not hard-coded here.

locals {
  base_labels = merge(
    {
      company_id = var.company_id
      env        = var.env
      managed_by = "terraform"
    },
    var.labels,
  )

  name_prefix = "${var.company_id}-${var.env}"
}

resource "google_project_service" "enabled" {
  for_each = toset(var.enabled_apis)

  project = var.project_id
  service = each.value

  disable_on_destroy = false
}

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = var.runtime_service_account_id
  display_name = "SanMai runtime SA (${local.name_prefix})"

  depends_on = [google_project_service.enabled]
}
