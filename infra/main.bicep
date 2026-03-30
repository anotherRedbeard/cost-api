param environmentName string
param location string = resourceGroup().location
@description('Optional default subscription ID that the function uses when subscriptionId is not supplied in the request.')
param targetCostSubscriptionId string = ''
@allowed([
  'MonthToDate'
  'TheLastMonth'
  'TheLastWeek'
  'TheLastYear'
  'WeekToDate'
  'BillingMonthToDate'
  'TheLastBillingMonth'
])
param defaultTimeframe string = 'MonthToDate'
@allowed([
  'Daily'
  'None'
])
param defaultGranularity string = 'Daily'

var suffix = uniqueString(subscription().subscriptionId, resourceGroup().id, environmentName)
var functionAppName = take('func-${environmentName}-cost-${suffix}', 60)
var storageAccountName = toLower(take('st${suffix}', 24))
var hostingPlanName = 'plan-${environmentName}-cost'
var appInsightsName = take('appi-${environmentName}-cost-${suffix}', 60)
var workspaceName = take('log-${environmentName}-cost-${suffix}', 63)
var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${listKeys(storageAccount.id, '2023-05-01').keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
var tags = {
  'azd-env-name': environmentName
  app: 'cost-api-demo'
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalyticsWorkspace.id
  }
}

resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: hostingPlanName
  location: location
  tags: tags
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  kind: 'functionapp'
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  tags: union(tags, {
    'azd-service-name': 'api'
  })
  properties: {
    httpsOnly: true
    reserved: true
    serverFarmId: hostingPlan.id
    siteConfig: {
      alwaysOn: false
      ftpsState: 'Disabled'
      linuxFxVersion: 'Python|3.11'
      minTlsVersion: '1.2'
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: storageConnectionString
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: applicationInsights.properties.ConnectionString
        }
        {
          name: 'AzureWebJobsFeatureFlags'
          value: 'EnableWorkerIndexing'
        }
        {
          name: 'COST_QUERY_GRANULARITY'
          value: defaultGranularity
        }
        {
          name: 'COST_QUERY_TIMEFRAME'
          value: defaultTimeframe
        }
        {
          name: 'COST_SUBSCRIPTION_ID'
          value: targetCostSubscriptionId
        }
        {
          name: 'ENABLE_ORYX_BUILD'
          value: 'true'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'PYTHON_ENABLE_INIT_INDEXING'
          value: '1'
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
      ]
    }
  }
}

output AZURE_FUNCTION_APP_NAME string = functionApp.name
output FUNCTION_APP_PRINCIPAL_ID string = functionApp.identity.principalId
output SERVICE_API_ENDPOINT_URL string = 'https://${functionApp.properties.defaultHostName}/api'
