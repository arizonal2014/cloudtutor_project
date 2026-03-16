# Infra

Session 10 adds production deployment assets:
- `infra/terraform/` for Cloud Run + IAM + optional Firestore/Storage provisioning
- `infra/env/cloudrun.env.yaml.example` for Cloud Run runtime environment values

Primary docs:
- [infra/terraform/README.md](/Users/user/Documents/projects/CloudTutor/infra/terraform/README.md)

Primary deploy helper:
- `scripts/deploy_cloud_run.sh`
  - Bootstraps dedicated runtime service account + IAM in gcloud deploy mode.
