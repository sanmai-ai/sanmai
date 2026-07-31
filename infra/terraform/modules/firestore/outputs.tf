output "database_id" {
  description = "Firestore database id."
  value       = google_firestore_database.this.name
}

output "database_uid" {
  description = "System-generated UID of the Firestore database."
  value       = google_firestore_database.this.uid
}

output "pitr_enabled" {
  description = "Whether Firestore PITR is enabled."
  value       = var.pitr_enabled
}

output "scheduled_export_bucket" {
  description = "GCS bucket scheduled exports write to."
  value       = var.scheduled_export_bucket
}

output "export_jobs" {
  description = "Names of the scheduled export jobs (empty when no export endpoint is configured)."
  value = local.create_export_jobs ? [
    google_cloud_scheduler_job.money_spine_export[0].name,
    google_cloud_scheduler_job.full_export[0].name,
  ] : []
}
