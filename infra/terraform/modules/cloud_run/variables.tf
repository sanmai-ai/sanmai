variable "project_id" {
  description = "GCP project id that hosts the Cloud Run service."
  type        = string
}

variable "region" {
  description = "Region for the Cloud Run service (e.g. \"me-west1\")."
  type        = string
}

variable "service_name" {
  description = "Name of the Cloud Run service."
  type        = string
}

variable "image" {
  description = "Fully-qualified container image to deploy (e.g. \"REGION-docker.pkg.dev/PROJECT/repo/img:tag\")."
  type        = string
}

variable "service_account_email" {
  description = "Runtime service account the service executes as."
  type        = string
}

variable "env_vars" {
  description = "Plain (non-secret) environment variables for the container."
  type        = map(string)
  default     = {}
}

variable "secret_env_vars" {
  description = "Env vars sourced from Secret Manager: name => { secret = <secret id>, version = <version or \"latest\"> }."
  type = map(object({
    secret  = string
    version = string
  }))
  default = {}
}

variable "cpu" {
  description = "CPU limit per instance."
  type        = string
  default     = "1"
}

variable "memory" {
  description = "Memory limit per instance."
  type        = string
  default     = "512Mi"
}

variable "min_instances" {
  description = "Minimum number of instances (0 = scale to zero)."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Maximum number of instances."
  type        = number
  default     = 10
}

variable "allow_unauthenticated" {
  description = "If true, grant roles/run.invoker to allUsers (public ingress)."
  type        = bool
  default     = false
}

variable "vpc_connector" {
  description = "Optional Serverless VPC Access connector id for egress (e.g. Cloud SQL private IP). Null = no connector."
  type        = string
  default     = null
}

variable "cloudsql_instances" {
  description = "Cloud SQL instance connection names to mount via the Cloud SQL proxy volume."
  type        = list(string)
  default     = []
}

variable "labels" {
  description = "Labels applied to the service."
  type        = map(string)
  default     = {}
}
