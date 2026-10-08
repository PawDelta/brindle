# Deployment templates

One template per cloud lets your organization trust brindle CI from its own
cloud with no stored secret. Each one trusts only GitHub's OIDC token
(issuer `https://token.actions.githubusercontent.com`) for the GitHub
environment `brindle-ci` of one repository, i.e. the subject
`repo:OWNER/REPO:environment:brindle-ci`.

## Tightening trust

The run job in the `brindle-ci` environment can mint cloud credentials for
code from branches of the same repository (forks never get OIDC tokens). Keep
the cloud role narrow: the templates grant only Claude model calls, so leave
it that way. If you need more control, add protection rules to the
`brindle-ci` environment (required reviewers, or limit it to selected
branches).

## AWS (Amazon Bedrock)

[`aws/brindle-ci-bedrock.yaml`](aws/brindle-ci-bedrock.yaml) is a
CloudFormation template. Each release uploads it to an S3 bucket under
`templates/<version>/brindle-ci-bedrock.yaml`; CloudFormation quick-create
only accepts S3 URLs, so open
`https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?templateURL=<S3 URL of that file>&stackName=brindle-ci`,
or from a checkout:

```sh
aws cloudformation deploy --template-file deploy/aws/brindle-ci-bedrock.yaml \
  --stack-name brindle-ci --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubRepo=OWNER/REPO CreateOidcProvider=true
```

Set `CreateOidcProvider=false` if the account already has the GitHub OIDC
provider.

Then let `brindle ci init` read the outputs itself:

```sh
brindle ci init --from-stack brindle-ci [--region us-east-1]
```

| Output | `brindle ci init` |
| --- | --- |
| `RoleArn` | the AWS role to assume (`AWS_ROLE_ARN`; the region comes from the stack) |

## Google Cloud (Vertex AI)

[`gcp/main.tf`](gcp/main.tf) is Terraform. In Cloud Shell, with the repository
open, follow [`gcp/tutorial.md`](gcp/tutorial.md), or:

```sh
cd deploy/gcp
terraform init
terraform apply -var project_id=PROJECT -var github_repo=OWNER/REPO
```

Then let `brindle ci init` read the outputs itself:

```sh
brindle ci init --from-terraform deploy/gcp
```

| Output | `brindle ci init` |
| --- | --- |
| `workload_identity_provider` | the workload identity provider |
| `service_account_email` | the service account (the project id comes from it) |

## Azure (Foundry)

[`azure/main.bicep`](azure/main.bicep) is the source and
[`azure/azuredeploy.json`](azure/azuredeploy.json) its compiled form (rebuild
it with `az bicep build --file deploy/azure/main.bicep --outfile
deploy/azure/azuredeploy.json` after changing the Bicep). Deploy into the
resource group of your existing Foundry resource:

```sh
az deployment group create --resource-group RG \
  --template-file deploy/azure/main.bicep \
  --parameters githubRepo=OWNER/REPO foundryAccountName=FOUNDRY_NAME
```

Or use the Deploy to Azure button: open
`https://portal.azure.com/#create/Microsoft.Template/uri/<URL-encoded raw URL of azuredeploy.json>`.

Then let `brindle ci init` read the outputs itself (the deployment is named
after the template file, `main`, unless you passed `--name`):

```sh
brindle ci init --from-deployment main -g RG
```

| Output | `brindle ci init` |
| --- | --- |
| `clientId` | the Azure client id |
| `tenantId` | the Azure tenant id |
| `subscriptionId` | not used |
