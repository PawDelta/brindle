# Trust brindle CI from Google Cloud

## Pick the project

Set the project brindle CI should use for Vertex AI:

```sh
gcloud config set project <walkthrough-project-id/>
```

## Apply the template

Replace `OWNER/REPO` with the GitHub repository brindle CI runs in:

```sh
cd deploy/gcp
terraform init
terraform apply -var "project_id=$(gcloud config get-value project)" -var "github_repo=OWNER/REPO"
```

This enables the Vertex AI, IAM Credentials and STS APIs, creates the
`brindle-ci` workload identity pool and a `github` provider that only accepts
the GitHub environment `brindle-ci` of that repository, and a service account
with the Vertex AI User role.

## Use the outputs

Give `workload_identity_provider` and `service_account_email` to
`brindle ci init`. No key is created or stored.
