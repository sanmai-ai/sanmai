variable "project_id" {
  description = "GCP project id that hosts the Cloud SQL instance."
  type        = string
}

variable "region" {
  description = "Region for the instance. For IL residency backups/PITR stay in-region (e.g. \"me-west1\")."
  type        = string
}

variable "instance_name" {
  description = "Name of the Cloud SQL instance."
  type        = string
}

variable "database_version" {
  description = "Cloud SQL Postgres engine version."
  type        = string
  default     = "POSTGRES_15"
}

variable "tier" {
  description = "Machine tier for the instance (e.g. \"db-custom-2-7680\")."
  type        = string
  default     = "db-custom-1-3840"
}

variable "availability_type" {
  description = "\"ZONAL\" or \"REGIONAL\" (REGIONAL = HA failover)."
  type        = string
  default     = "ZONAL"
}

variable "disk_autoresize" {
  description = "Whether the data disk auto-resizes."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Guards the instance against accidental terraform destroy."
  type        = bool
  default     = true
}

# --- Backup / DR knobs (acceptance-required) -------------------------------

variable "pitr_enabled" {
  description = "Enable Cloud SQL point-in-time recovery (transaction-log replay). Drives the money-path <=1 min RPO."
  type        = bool
  default     = true
}

variable "backup_retention_days" {
  description = "Number of automated backups retained. See backup-and-dr.md retention table (35 on prod, 7 elsewhere)."
  type        = number
  default     = 7
}

variable "transaction_log_retention_days" {
  description = "Days of transaction logs retained for PITR (1-7). Requires pitr_enabled."
  type        = number
  default     = 7

  validation {
    condition     = var.transaction_log_retention_days >= 1 && var.transaction_log_retention_days <= 7
    error_message = "transaction_log_retention_days must be between 1 and 7."
  }
}

variable "backup_start_time" {
  description = "HH:MM UTC start time for the daily automated backup window."
  type        = string
  default     = "01:00"
}

# --- Networking / encryption ------------------------------------------------

variable "private_network" {
  description = "Optional VPC self_link for private IP. Null = public IP only (dev)."
  type        = string
  default     = null
}

variable "kms_key_name" {
  description = "Optional CMEK key for encryption at rest. Null = Google-managed key."
  type        = string
  default     = null
}

variable "labels" {
  description = "User labels applied to the instance."
  type        = map(string)
  default     = {}
}
