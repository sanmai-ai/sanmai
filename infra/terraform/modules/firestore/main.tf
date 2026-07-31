# firestore — parameterized Firestore database skeleton with PITR and
# scheduled managed exports to the cross-project vault bucket.

locals {
  pitr_enablement = var.pitr_enabled ? "POINT_IN_TIME_RECOVERY_ENABLED" : "POINT_IN_TIME_RECOVERY_DISABLED"

  # Only create scheduler jobs when an export endpoint is supplied.
  create_export_jobs = var.export_scheduler_uri != null
}

resource "google_firestore_database" "this" {
  project                           = var.project_id
  name                              = var.database_id
  location_id                       = var.location_id
  type                              = var.database_type
  database_edition                  = var.database_edition
  point_in_time_recovery_enablement = local.pitr_enablement
  deletion_policy                   = var.deletion_policy
}

# Hourly money-spine export (orders + counters) -> control-plane vault bucket.
resource "google_cloud_scheduler_job" "money_spine_export" {
  count = local.create_export_jobs ? 1 : 0

  project     = var.project_id
  region      = var.location_id
  name        = "firestore-export-money-spine"
  description = "Hourly Firestore money-spine export to ${var.scheduled_export_bucket}"
  schedule    = var.money_spine_export_schedule
  time_zone   = var.export_time_zone

  http_target {
    http_method = "POST"
    uri         = var.export_scheduler_uri
    body = base64encode(jsonencode({
      database    = google_firestore_database.this.name
      bucket      = var.scheduled_export_bucket
      collections = ["orders", "counters"]
    }))

    oidc_token {
      service_account_email = var.export_scheduler_sa_email
    }
  }
}

# Daily full-DB export (all collections incl. customers) -> vault bucket.
resource "google_cloud_scheduler_job" "full_export" {
  count = local.create_export_jobs ? 1 : 0

  project     = var.project_id
  region      = var.location_id
  name        = "firestore-export-full"
  description = "Daily full Firestore export to ${var.scheduled_export_bucket}"
  schedule    = var.full_export_schedule
  time_zone   = var.export_time_zone

  http_target {
    http_method = "POST"
    uri         = var.export_scheduler_uri
    body = base64encode(jsonencode({
      database    = google_firestore_database.this.name
      bucket      = var.scheduled_export_bucket
      collections = []
    }))

    oidc_token {
      service_account_email = var.export_scheduler_sa_email
    }
  }
}
