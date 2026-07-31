variable "project_id" {
  description = "Control-plane project id (tier-0). Holds the routing registry + secret vault; zero tenant runtime data."
  type        = string
}

variable "region" {
  description = "Region for control-plane resources (e.g. \"me-west1\")."
  type        = string
}

variable "enabled_apis" {
  description = "APIs to enable on the control-plane project."
  type        = list(string)
  default = [
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "cloudkms.googleapis.com",
    "run.googleapis.com",
  ]
}

variable "registry_secret_id" {
  description = "Secret Manager secret id holding the routing registry connection string. Control-plane stores connection STRINGS, never credentials (S11/S12)."
  type        = string
  default     = "control-plane-registry-conn"
}

variable "restore_operator_account_id" {
  description = "Account id (local part) for the dedicated restore-operator service account (separate from deploy SAs)."
  type        = string
  default     = "restore-operator"
}

variable "kms_key_ring_name" {
  description = "KMS key ring name for control-plane CMEK keys (vault + secret export)."
  type        = string
  default     = "control-plane-vault"
}

variable "vault_kms_key_name" {
  description = "KMS crypto key name used to wrap vault objects."
  type        = string
  default     = "vault-cmek"
}

variable "labels" {
  description = "Labels applied to control-plane resources."
  type        = map(string)
  default     = {}
}
