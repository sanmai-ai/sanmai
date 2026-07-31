# control_plane — tier-0 shared foundation: routing registry secret, the
# restore-operator identity, and the CMEK key ring backing the backup vault.
# The vault bucket itself is provisioned by the backup_vault module and wired
# to the key + SA this module exposes.

resource "google_project_service" "enabled" {
  for_each = toset(var.enabled_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# Dedicated restore-operator SA — separate from deploy/runtime SAs.
resource "google_service_account" "restore_operator" {
  project      = var.project_id
  account_id   = var.restore_operator_account_id
  display_name = "SanMai restore-operator (control-plane)"

  depends_on = [google_project_service.enabled]
}

# CMEK key ring + key backing the vault and secret-value exports.
resource "google_kms_key_ring" "vault" {
  project  = var.project_id
  name     = var.kms_key_ring_name
  location = var.region

  depends_on = [google_project_service.enabled]
}

resource "google_kms_crypto_key" "vault" {
  name     = var.vault_kms_key_name
  key_ring = google_kms_key_ring.vault.id

  # Vault objects are long-lived; do not auto-destroy the key with the resource.
  lifecycle {
    prevent_destroy = true
  }
}

# Routing registry connection string — a STRING, not credentials (S11/S12).
resource "google_secret_manager_secret" "registry" {
  project   = var.project_id
  secret_id = var.registry_secret_id
  labels    = var.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}
