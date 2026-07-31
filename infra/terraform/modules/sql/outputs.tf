output "instance_name" {
  description = "Name of the Cloud SQL instance."
  value       = google_sql_database_instance.this.name
}

output "connection_name" {
  description = "Instance connection name (project:region:instance) for the Cloud SQL proxy / Unix socket."
  value       = google_sql_database_instance.this.connection_name
}

output "self_link" {
  description = "Self link of the instance."
  value       = google_sql_database_instance.this.self_link
}

output "private_ip_address" {
  description = "Private IP of the instance (empty if no private network)."
  value       = google_sql_database_instance.this.private_ip_address
}

output "pitr_enabled" {
  description = "Whether PITR is enabled on this instance."
  value       = var.pitr_enabled
}
