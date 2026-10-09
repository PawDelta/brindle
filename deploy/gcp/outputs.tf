output "workload_identity_provider" {
  description = "Provider resource name (the workload identity provider in `brindle ci init`)."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "service_account_email" {
  description = "Service account for brindle CI to impersonate (the service account in `brindle ci init`)."
  value       = google_service_account.brindle_ci.email
}
