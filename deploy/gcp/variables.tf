variable "project_id" {
  type        = string
  description = "The Google Cloud project whose Vertex AI brindle CI uses."
}

variable "github_repo" {
  type        = string
  description = "The GitHub repository brindle CI runs in, as owner/repo."

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "Use the form owner/repo."
  }
}
