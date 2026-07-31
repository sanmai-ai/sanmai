# backup_vault — hardened cross-project GCS vault for Cloud SQL pg_dumps,
# Firestore exports, and encrypted secret-value copies.
# Properties (backup-and-dr.md §6): retention-LOCK, CMEK, restore-operator IAM,
# append-only writers (no delete), uniform bucket-level access.

resource "google_storage_bucket" "vault" {
  project       = var.project_id
  name          = var.bucket_name
  location      = var.location
  force_destroy = var.force_destroy
  labels        = var.labels

  # Deny ad-hoc ACLs — IAM is the single source of access truth.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = var.enable_versioning
  }

  # Retention-lock: objects are immutable until the period elapses.
  retention_policy {
    is_locked        = var.retention_lock_enabled
    retention_period = var.retention_period_days * 24 * 60 * 60
  }

  # CMEK — every object wrapped with the caller-supplied key.
  encryption {
    default_kms_key_name = var.kms_key_name
  }
}

# Restore-operator: the sole cross-project reader of vault objects.
resource "google_storage_bucket_iam_member" "restore_operator" {
  bucket = google_storage_bucket.vault.name
  role   = var.restore_operator_role
  member = var.restore_operator_member
}

# Backup writers: create-only, no delete (retention-lock + IAM => append-only).
resource "google_storage_bucket_iam_member" "writers" {
  for_each = toset(var.writer_members)

  bucket = google_storage_bucket.vault.name
  role   = "roles/storage.objectCreator"
  member = each.value
}
