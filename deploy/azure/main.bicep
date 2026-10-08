// Lets brindle CI in one GitHub repository call Azure AI Foundry with no stored
// secret: GitHub's OIDC token, from the brindle-ci environment, is exchanged for
// this managed identity's token. Deploy into the resource group that holds the
// Foundry resource.

@description('The GitHub repository brindle CI runs in, as owner/repo.')
param githubRepo string

@description('Name of the existing Foundry (Azure AI Services) resource in this resource group.')
param foundryAccountName string

@description('Region for the managed identity.')
param location string = resourceGroup().location

var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource foundry 'Microsoft.CognitiveServices/accounts@2023-05-01' existing = {
  name: foundryAccountName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'brindle-ci'
  location: location
}

resource federatedCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'github-brindle-ci'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubRepo}:environment:brindle-ci'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, identity.id, cognitiveServicesUserRoleId)
  scope: foundry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
  }
}

output clientId string = identity.properties.clientId
output tenantId string = tenant().tenantId
output subscriptionId string = subscription().subscriptionId
