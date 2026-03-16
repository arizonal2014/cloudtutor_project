output "service_name" {
  value       = google_cloud_run_v2_service.backend.name
  description = "Cloud Run service name."
}

output "service_url" {
  value       = google_cloud_run_v2_service.backend.uri
  description = "Public URL of the deployed backend."
}

output "container_image" {
  value       = local.image_uri
  description = "Container image deployed to Cloud Run."
}

output "service_account_email" {
  value       = google_service_account.backend.email
  description = "Service account used by Cloud Run runtime."
}

output "artifact_registry_repository" {
  value       = var.artifact_repo
  description = "Artifact Registry repository id for backend images."
}

output "artifact_bucket_name" {
  value       = var.create_artifact_bucket ? google_storage_bucket.artifacts[0].name : null
  description = "Artifact bucket name when create_artifact_bucket is enabled."
}
