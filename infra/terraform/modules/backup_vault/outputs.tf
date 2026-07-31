output "bucket_name" {
  description = "Name of the vault bucket."
  value       = google_storage_bucket.vault.name
}

output "bucket_url" {
  description = "gs:// URL of the vault bucket."
  value       = google_storage_bucket.vault.url
}

output "self_link" {
  description = "Self link of the vault bucket."
  value       = google_storage_bucket.vault.self_link
}

output "retention_period_seconds" {
  description = "Retention-lock period in seconds."
  value       = var.retention_period_days * 24 * 60 * 60
}

output "restore_operator_member" {
  description = "IAM member granted restore/read access."
  value       = var.restore_operator_member
}
