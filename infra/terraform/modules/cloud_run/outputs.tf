output "service_name" {
  description = "Name of the Cloud Run service."
  value       = google_cloud_run_v2_service.this.name
}

output "service_uri" {
  description = "HTTPS URL of the deployed service."
  value       = google_cloud_run_v2_service.this.uri
}

output "latest_revision" {
  description = "Name of the latest created revision."
  value       = google_cloud_run_v2_service.this.latest_created_revision
}
