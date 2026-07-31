variable "project_id" {
  description = "Control-plane project id that owns the vault bucket (holds zero tenant runtime data)."
  type        = string
}

variable "location" {
  description = "GCS location for the vault. For IL tenants keep in-region (e.g. \"me-west1\") — no cross-region copy out of Israel."
  type        = string
}

variable "bucket_name" {
  description = "Globally-unique name of the vault bucket. Supplied by the caller; never a hard-coded sanmai-* literal."
  type        = string
}

variable "retention_period_days" {
  description = "Retention-lock period in days. Objects cannot be deleted/overwritten before this elapses (e.g. 90 on prod). See backup-and-dr.md retention table."
  type        = number
  default     = 90

  validation {
    condition     = var.retention_period_days >= 1
    error_message = "retention_period_days must be at least 1."
  }
}

variable "retention_lock_enabled" {
  description = "Whether to LOCK the retention policy. WARNING: locking is irreversible and permanently prevents shortening/removing retention."
  type        = bool
  default     = true
}

variable "kms_key_name" {
  description = "CMEK key resource id used to encrypt every object in the vault (not Google-default keys). Required by the DR plan."
  type        = string
}

variable "restore_operator_member" {
  description = "IAM member (e.g. \"serviceAccount:restore-operator@PROJECT.iam.gserviceaccount.com\") granted read/restore access. The sole cross-project reader of vault objects."
  type        = string
}

variable "restore_operator_role" {
  description = "Role bound to the restore-operator member. Read-only by default so the operator cannot delete backups (delete is denied to all company SAs)."
  type        = string
  default     = "roles/storage.objectViewer"
}

variable "writer_members" {
  description = "IAM members allowed to WRITE new backup objects (export/pg_dump jobs). They get objectCreator only — no delete — so retention-lock + IAM together make the vault append-only."
  type        = list(string)
  default     = []
}

variable "enable_versioning" {
  description = "Enable object versioning on the vault bucket."
  type        = bool
  default     = true
}

variable "force_destroy" {
  description = "Allow terraform to delete a non-empty bucket. Keep false for real vaults — a locked retention policy will block destroy regardless."
  type        = bool
  default     = false
}

variable "labels" {
  description = "Labels applied to the bucket."
  type        = map(string)
  default     = {}
}
