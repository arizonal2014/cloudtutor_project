# CloudTutor Terraform (Session 10)

This Terraform stack provisions CloudTutor backend runtime infrastructure:
- required GCP APIs
- Artifact Registry Docker repository
- Cloud Run v2 backend service
- runtime service account + IAM roles
- optional Cloud Storage artifact bucket
- optional Firestore default database

## Prerequisites
- Terraform `>= 1.5`
- `gcloud` authenticated with access to target project

## Quick Start
1. Copy vars file:
```bash
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
```

2. Adjust values in `infra/terraform/terraform.tfvars`.

3. Initialize and apply:
```bash
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform apply
```

4. Read outputs:
```bash
terraform -chdir=infra/terraform output
```

## Notes
- `container_image` can be set to a fully qualified image URI to deploy an already-built image.
- Keep `create_firestore_database=false` when Firestore is already initialized by Firebase.
- Cloud Run websocket traffic is supported over HTTPS; frontend clients should use `wss://` URLs in production.
