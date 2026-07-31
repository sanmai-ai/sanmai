# SanMai AI — Terraform modules

Parameterized, reusable building blocks for standing up a company × env on GCP.
These are **modules only** — no `envs/`, no `sanmai-*` names, no real project ids.
The calling env (`infra/terraform/envs/<...>/`, not in this repo/foundation) supplies
project ids, regions, names, and tfvars and composes these modules together.

Design invariants (see `docs/architecture/backup-and-dr.md`,
`multi-tenant-redesign-plan.md`):

- One company = its own project(s); IL tenants keep all data/backups in region
  (e.g. `me-west1`) — no cross-region copy out of Israel.
- Control-plane stores connection **strings**, never credentials.
- Backups are append-only and CMEK-encrypted; only a dedicated restore-operator
  can read the vault.

## Modules

| Module | Purpose | Key required variables |
|---|---|---|
| `company_env` | Baseline per-company×env scaffold: enable APIs, runtime SA, naming/labels. | `company_id`, `env`, `project_id`, `region` |
| `cloud_run` | Cloud Run (v2) service with env/secret vars, VPC connector, Cloud SQL mounts. | `project_id`, `region`, `service_name`, `image`, `service_account_email` |
| `sql` | Cloud SQL Postgres instance with automated backups + PITR. | `backup_retention_days`, `transaction_log_retention_days`, `pitr_enabled` |
| `firestore` | Firestore database (Enterprise) with PITR + scheduled exports to the vault. | `scheduled_export_bucket`, `pitr_enabled` |
| `control_plane` | Tier-0 shared foundation: routing-registry secret, restore-operator SA, vault CMEK key ring. | `project_id`, `region` |
| `backup_vault` | Hardened cross-project GCS vault: retention-lock + CMEK + restore-operator IAM. | `bucket_name`, `kms_key_name`, `restore_operator_member` |

## Composition sketch

```hcl
module "control_plane" {
  source     = "../../modules/control_plane"
  project_id = var.control_project_id
  region     = var.region
}

module "backup_vault" {
  source                  = "../../modules/backup_vault"
  project_id              = var.control_project_id
  location                = var.region
  bucket_name             = var.vault_bucket_name
  retention_period_days   = 90
  kms_key_name            = module.control_plane.vault_kms_key_id
  restore_operator_member = module.control_plane.restore_operator_member
}

module "company" {
  source     = "../../modules/company_env"
  company_id = var.company_id
  env        = var.env
  project_id = var.project_id
  region     = var.region
}

module "db" {
  source                         = "../../modules/sql"
  project_id                     = module.company.project_id
  region                         = module.company.region
  instance_name                  = "${module.company.name_prefix}-pg"
  pitr_enabled                   = true
  backup_retention_days          = 35
  transaction_log_retention_days = 7
}

module "firestore" {
  source                  = "../../modules/firestore"
  project_id              = module.company.project_id
  location_id             = module.company.region
  pitr_enabled            = true
  scheduled_export_bucket = module.backup_vault.bucket_name
}

module "api" {
  source                = "../../modules/cloud_run"
  project_id            = module.company.project_id
  region                = module.company.region
  service_name          = "${module.company.name_prefix}-be"
  image                 = var.be_image
  service_account_email = module.company.runtime_service_account_email
  cloudsql_instances    = [module.db.connection_name]
}
```

## Validation

These are skeletons: valid HCL against provider `hashicorp/google`, intended for
`terraform validate` / `plan` against a dummy project. **No `apply`** is required
or expected in the foundation.

```sh
cd infra/terraform/modules/<module>
terraform init -backend=false
terraform validate
```
