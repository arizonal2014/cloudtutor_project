variable "project_id" {
  description = "GCP project id for CloudTutor deployment."
  type        = string
}

variable "region" {
  description = "Primary region for Cloud Run and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "cloudtutor-backend"
}

variable "artifact_repo" {
  description = "Artifact Registry repository id used for Docker images."
  type        = string
  default     = "cloudtutor-images"
}

variable "image_name" {
  description = "Container image name inside Artifact Registry."
  type        = string
  default     = "cloudtutor-backend"
}

variable "image_tag" {
  description = "Container image tag when container_image is not explicitly set."
  type        = string
  default     = "latest"
}

variable "container_image" {
  description = "Optional fully-qualified image uri. When set, this overrides region/repo/image_name/image_tag composition."
  type        = string
  default     = ""
}

variable "allow_unauthenticated" {
  description = "Whether to grant public invoker access to the Cloud Run service."
  type        = bool
  default     = true
}

variable "cpu" {
  description = "CPU limit per Cloud Run instance."
  type        = string
  default     = "2"
}

variable "memory" {
  description = "Memory limit per Cloud Run instance."
  type        = string
  default     = "2Gi"
}

variable "min_instances" {
  description = "Minimum number of Cloud Run instances."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Maximum number of Cloud Run instances."
  type        = number
  default     = 3
}

variable "max_instance_request_concurrency" {
  description = "Max concurrent requests handled by each Cloud Run instance."
  type        = number
  default     = 32
}

variable "enable_apis" {
  description = "Enable required Google APIs during apply."
  type        = bool
  default     = true
}

variable "create_artifact_registry" {
  description = "Create Artifact Registry repository if true."
  type        = bool
  default     = true
}

variable "service_account_id" {
  description = "Service account id for Cloud Run runtime identity."
  type        = string
  default     = "cloudtutor-backend-sa"
}

variable "env_vars" {
  description = "Environment variables passed to Cloud Run container."
  type        = map(string)
  default     = {}
}

variable "create_artifact_bucket" {
  description = "Create a Cloud Storage bucket for artifacts."
  type        = bool
  default     = false
}

variable "artifact_bucket_name" {
  description = "Name for artifact bucket when create_artifact_bucket=true."
  type        = string
  default     = "cloudtutor-artifacts"
}

variable "artifact_bucket_location" {
  description = "Location for artifact bucket."
  type        = string
  default     = "US"
}

variable "create_firestore_database" {
  description = "Create Firestore default database via Terraform. Keep false if Firebase already initialized this project."
  type        = bool
  default     = false
}

variable "firestore_location" {
  description = "Firestore database location when create_firestore_database=true."
  type        = string
  default     = "nam5"
}

variable "additional_service_account_roles" {
  description = "Additional project roles to grant Cloud Run service account."
  type        = list(string)
  default     = []
}
