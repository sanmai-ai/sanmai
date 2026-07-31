output "project_id" {
  description = "Control-plane project id."
  value       = var.project_id
}

output "restore_operator_email" {
  description = "Email of the restore-operator service account (bind to the backup_vault module)."
  value       = google_service_account.restore_operator.email
}

output "restore_operator_member" {
  description = "IAM member string for the restore-operator SA."
  value       = "serviceAccount:${google_service_account.restore_operator.email}"
}

output "vault_kms_key_id" {
  description = "CMEK key id backing the vault and secret exports (pass to backup_vault.kms_key_name)."
  value       = google_kms_crypto_key.vault.id
}

output "registry_secret_id" {
  description = "Secret Manager secret id holding the routing registry connection string."
  value       = google_secret_manager_secret.registry.secret_id
}
