// Supporting infrastructure for the Hisense TV Sports AI Assistant POC.
//
// Scope: resource group. Deploy standalone with:
//   az group create -n <rg> -l <location>
//   az deployment group create -g <rg> -f infra/main.bicep -p infra/main.parameters.json
//
// This provisions the *supporting* resources the demo uses:
//   - Azure AI Search        -> backs Feature 1 (Foundry IQ knowledge base)
//   - Log Analytics + App Insights -> backs Feature 4 (evaluation & monitoring / tracing)
//
// The Foundry project, model deployment, and agent container host are created
// by `azd ai agent` / the foundry project-create flow (see poc/README.md); they
// are intentionally NOT recreated here so this file never collides with azd.

@description('Short name used to derive resource names. Lowercase letters/numbers.')
@minLength(3)
@maxLength(18)
param namePrefix string = 'hisensepoc'

@description('Azure region for the supporting resources.')
param location string = resourceGroup().location

@description('Azure AI Search SKU. "basic" is enough for the POC index volume.')
@allowed([
  'basic'
  'standard'
  'standard2'
])
param searchSku string = 'basic'

@description('Log Analytics retention in days.')
@minValue(30)
@maxValue(730)
param logRetentionInDays int = 30

@description('Tags applied to every resource.')
param tags object = {
  project: 'hisense-tv-assistant-poc'
  scenario: 'sports-ai-assistant'
}

var suffix = uniqueString(resourceGroup().id, namePrefix)
var searchName = toLower('${namePrefix}-search-${suffix}')
var logAnalyticsName = toLower('${namePrefix}-logs-${suffix}')
var appInsightsName = toLower('${namePrefix}-appi-${suffix}')

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: searchName
  location: location
  tags: tags
  sku: {
    name: searchSku
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    semanticSearch: 'standard'
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
  }
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionInDays
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
  }
}

@description('Set this on the agent as FOUNDRY_IQ_ENDPOINT.')
output FOUNDRY_IQ_ENDPOINT string = 'https://${search.name}.search.windows.net'

@description('AI Search resource name (Foundry IQ knowledge store).')
output SEARCH_SERVICE_NAME string = search.name

@description('Set this on the agent / azd env as APPLICATIONINSIGHTS_CONNECTION_STRING.')
output APPLICATIONINSIGHTS_CONNECTION_STRING string = appInsights.properties.ConnectionString

@description('App Insights resource name (evaluation & monitoring).')
output APP_INSIGHTS_NAME string = appInsights.name

@description('Log Analytics workspace resource id.')
output LOG_ANALYTICS_WORKSPACE_ID string = logAnalytics.id
