output "project_id" {
  description = "GCP project id for this company x env."
  value       = var.project_id
}

output "region" {
  description = "Primary region for this company x env."
  value       = var.region
}

output "name_prefix" {
  description = "Canonical <company_id>-<env> prefix for naming child resources."
  value       = local.name_prefix
}

output "labels" {
  description = "Merged label set to apply to child resources."
  value       = local.base_labels
}

output "runtime_service_account_email" {
  description = "Email of the runtime service account for this company x env."
  value       = google_service_account.runtime.email
}

output "enabled_apis" {
  description = "APIs enabled on the project."
  value       = keys(google_project_service.enabled)
}
