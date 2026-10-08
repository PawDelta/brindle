# Lets brindle CI in one GitHub repository call Vertex AI with no stored
# secret: GitHub's OIDC token, from the brindle-ci environment, is exchanged
# through workload identity federation for the service account's credentials.

terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

provider "google" {
  project = var.project_id
}

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_iam_workload_identity_pool" "brindle_ci" {
  project                   = var.project_id
  workload_identity_pool_id = "brindle-ci"
  display_name              = "brindle CI"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.brindle_ci.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub Actions"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.sub == 'repo:${var.github_repo}:environment:brindle-ci'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "brindle_ci" {
  project      = var.project_id
  account_id   = "brindle-ci"
  display_name = "brindle CI"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.brindle_ci.email}"
}

resource "google_service_account_iam_member" "github_impersonation" {
  service_account_id = google_service_account.brindle_ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.brindle_ci.name}/attribute.repository/${var.github_repo}"
}
