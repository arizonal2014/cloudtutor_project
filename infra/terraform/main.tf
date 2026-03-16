locals {
  required_services = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "firestore.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "aiplatform.googleapis.com",
  ]

  image_uri = var.container_image != "" ? var.container_image : format(
    "%s-docker.pkg.dev/%s/%s/%s:%s",
    var.region,
    var.project_id,
    var.artifact_repo,
    var.image_name,
    var.image_tag,
  )

  base_env_vars = {
    CLOUDTUTOR_APP_NAME          = var.service_name
    CLOUDTUTOR_SESSION_STORE_DIR = "/tmp/cloudtutor/sessions"
    CLOUDTUTOR_ARTIFACT_DIR      = "/tmp/cloudtutor/artifacts"
  }

  artifact_bucket_env_vars = var.create_artifact_bucket ? {
    CLOUDTUTOR_ARTIFACT_GCS_BUCKET = google_storage_bucket.artifacts[0].name
  } : {}

  firestore_env_vars = var.create_firestore_database ? {
    CLOUDTUTOR_FIRESTORE_ENABLED = "1"
    FIRESTORE_PROJECT_ID         = var.project_id
  } : {}

  effective_env_vars = merge(
    local.base_env_vars,
    local.artifact_bucket_env_vars,
    local.firestore_env_vars,
    var.env_vars,
  )

  base_service_account_roles = [
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/secretmanager.secretAccessor",
    "roles/storage.objectAdmin",
    "roles/datastore.user",
    "roles/aiplatform.user",
  ]

  service_account_roles = toset(
    concat(local.base_service_account_roles, var.additional_service_account_roles)
  )
}

resource "google_project_service" "required" {
  for_each = var.enable_apis ? toset(local.required_services) : toset([])
  project  = var.project_id
  service  = each.value

  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  count         = var.create_artifact_registry ? 1 : 0
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repo
  description   = "CloudTutor backend container images"
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "artifacts" {
  count    = var.create_artifact_bucket ? 1 : 0
  name     = var.artifact_bucket_name
  project  = var.project_id
  location = var.artifact_bucket_location

  uniform_bucket_level_access = true
  force_destroy               = false

  depends_on = [google_project_service.required]
}

resource "google_firestore_database" "default" {
  count       = var.create_firestore_database ? 1 : 0
  project     = var.project_id
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"

  # Avoid accidental data destruction on terraform destroy.
  deletion_policy = "ABANDON"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "backend" {
  account_id   = var.service_account_id
  display_name = "CloudTutor Backend Runtime"
  project      = var.project_id
}

resource "google_project_iam_member" "backend_service_account_roles" {
  for_each = local.service_account_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.backend.email}"
}

resource "google_cloud_run_v2_service" "backend" {
  name     = var.service_name
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.backend.email
    timeout         = "300s"

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    max_instance_request_concurrency = var.max_instance_request_concurrency

    containers {
      image = local.image_uri

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
      }

      dynamic "env" {
        for_each = local.effective_env_vars
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  traffic {
    percent = 100
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
  }

  depends_on = [
    google_project_service.required,
    google_project_iam_member.backend_service_account_roles,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.backend.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
