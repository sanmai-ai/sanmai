# sql — parameterized Cloud SQL (Postgres) instance skeleton with the
# backup / PITR / transaction-log knobs the DR plan requires.

resource "google_sql_database_instance" "this" {
  project             = var.project_id
  region              = var.region
  name                = var.instance_name
  database_version    = var.database_version
  deletion_protection = var.deletion_protection

  encryption_key_name = var.kms_key_name

  settings {
    tier              = var.tier
    availability_type = var.availability_type
    disk_autoresize   = var.disk_autoresize
    user_labels       = var.labels

    backup_configuration {
      enabled                        = true
      start_time                     = var.backup_start_time
      point_in_time_recovery_enabled = var.pitr_enabled
      transaction_log_retention_days = var.transaction_log_retention_days

      backup_retention_settings {
        retained_backups = var.backup_retention_days
        retention_unit   = "COUNT"
      }
    }

    ip_configuration {
      ipv4_enabled    = var.private_network == null
      private_network = var.private_network
    }
  }
}
