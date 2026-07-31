variable "project_id" {
  description = "GCP project id that hosts the Firestore database."
  type        = string
}

variable "location_id" {
  description = "Firestore location (e.g. \"me-west1\" for IL residency)."
  type        = string
}

variable "database_id" {
  description = "Firestore database id. \"(default)\" or a named database."
  type        = string
  default     = "(default)"
}

variable "database_type" {
  description = "Firestore mode."
  type        = string
  default     = "FIRESTORE_NATIVE"
}

variable "database_edition" {
  description = "Firestore edition. ENTERPRISE is required for Firestore PITR (7-day window) per backup-and-dr.md."
  type        = string
  default     = "ENTERPRISE"
}

# --- Backup / DR knobs (acceptance-required) -------------------------------

variable "pitr_enabled" {
  description = "Enable Firestore point-in-time recovery (7-day window; needs ENTERPRISE edition)."
  type        = bool
  default     = true
}

variable "scheduled_export_bucket" {
  description = "GCS bucket (in the control-plane vault) that scheduled Firestore exports are written to. CMEK-encrypted, cross-project."
  type        = string
}

variable "money_spine_export_schedule" {
  description = "Cron for the hourly money-spine export (venues/*/orders + counters)."
  type        = string
  default     = "0 * * * *"
}

variable "full_export_schedule" {
  description = "Cron for the daily full-DB export (all collections incl. customers)."
  type        = string
  default     = "30 3 * * *"
}

variable "export_time_zone" {
  description = "Time zone for the export schedules."
  type        = string
  default     = "Asia/Jerusalem"
}

variable "export_scheduler_uri" {
  description = "HTTP endpoint (Cloud Run Job / Function) the scheduler hits to run gcloud firestore export. Null = do not create scheduler jobs (skeleton)."
  type        = string
  default     = null
}

variable "export_scheduler_sa_email" {
  description = "Service account used by the scheduler to invoke the export endpoint (OIDC). Required when export_scheduler_uri is set."
  type        = string
  default     = null
}

variable "deletion_policy" {
  description = "Deletion policy for the database resource (\"DELETE\" or \"ABANDON\")."
  type        = string
  default     = "ABANDON"
}
