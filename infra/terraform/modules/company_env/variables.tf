variable "company_id" {
  description = "Stable slug identifying the company/tenant (e.g. \"acme\"). Never hard-code a real tenant name; supplied by the calling env."
  type        = string
}

variable "env" {
  description = "Deployment environment for this company (e.g. \"prod\", \"stg\")."
  type        = string
}

variable "project_id" {
  description = "GCP project id that hosts this company x env. One company = its own project(s); passed in, never derived from a sanmai-* literal."
  type        = string
}

variable "region" {
  description = "Primary GCP region for this company x env (e.g. \"me-west1\" for IL residency)."
  type        = string
}

variable "enabled_apis" {
  description = "GCP service APIs to enable on the project for this env."
  type        = list(string)
  default = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudkms.googleapis.com",
    "storage.googleapis.com",
  ]
}

variable "runtime_service_account_id" {
  description = "Account id (local part) for the runtime service account created for this company x env."
  type        = string
  default     = "app-runtime"
}

variable "labels" {
  description = "Labels applied to resources in this company x env. company_id/env are merged in automatically."
  type        = map(string)
  default     = {}
}
