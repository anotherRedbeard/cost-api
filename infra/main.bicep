param environmentName string
param location string = resourceGroup().location
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
param defaultGranularity string = 'None'
@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 100
@allowed([
  2048
  4096
])
param instanceMemoryMB int = 2048

var suffix = uniqueString(subscription().subscriptionId, resourceGroup().id, environmentName)
var functionAppName = take('func-${environmentName}-cost-${suffix}', 60)
var storageAccountName = toLower(take('st${suffix}', 24))
var hostingPlanName = take('plan-${environmentName}-cost-${suffix}', 40)
var appInsightsName = take('appi-${environmentName}-cost-${suffix}', 60)
var workspaceName = take('log-${environmentName}-cost-${suffix}', 63)
var deploymentIdentityName = take('uai-${environmentName}-deploy-${suffix}', 64)
var deploymentStorageContainerName = take('app-package-${toLower(environmentName)}-${suffix}', 63)
var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var storageQueueDataContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
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
    allowSharedKeyAccess: false
    dnsEndpointType: 'Standard'
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    supportsHttpsTrafficOnly: true
  }

  resource blobServices 'blobServices' = {
    name: 'default'
    properties: {
      deleteRetentionPolicy: {}
    }

    resource deploymentContainer 'containers' = {
      name: deploymentStorageContainerName
      properties: {
        publicAccess: 'None'
      }
    }
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

resource deploymentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: deploymentIdentityName
  location: location
  tags: tags
}

resource blobOwnerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, storageAccount.id, deploymentIdentity.id, 'Storage Blob Data Owner')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleId)
    principalId: deploymentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource blobContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, storageAccount.id, deploymentIdentity.id, 'Storage Blob Data Contributor')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: deploymentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource queueContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, storageAccount.id, deploymentIdentity.id, 'Storage Queue Data Contributor')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContributorRoleId)
    principalId: deploymentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tableContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, storageAccount.id, deploymentIdentity.id, 'Storage Table Data Contributor')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributorRoleId)
    principalId: deploymentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource hostingPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: hostingPlanName
  location: location
  tags: tags
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  kind: 'functionapp'
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${deploymentIdentity.id}': {}
    }
  }
  tags: union(tags, {
    'azd-service-name': 'api'
  })
  properties: {
    httpsOnly: true
    serverFarmId: hostingPlan.id
    siteConfig: {
      minTlsVersion: '1.2'
      appSettings: []
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageAccount.properties.primaryEndpoints.blob}${deploymentStorageContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: deploymentIdentity.id
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
    }
  }
}

resource functionAppSettings 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: functionApp
  name: 'appsettings'
  properties: {
    APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsights.properties.ConnectionString
    AzureWebJobsFeatureFlags: 'EnableWorkerIndexing'
    AzureWebJobsStorage__accountName: storageAccount.name
    AzureWebJobsStorage__clientId: deploymentIdentity.properties.clientId
    AzureWebJobsStorage__credential: 'managedidentity'
    COST_QUERY_GRANULARITY: defaultGranularity
    COST_QUERY_TIMEFRAME: defaultTimeframe
    FUNCTIONS_EXTENSION_VERSION: '~4'
    MONTHLY_REPORT_BLOB_CONTAINER: 'monthly-cost-reports'
    MONTHLY_REPORT_DELIVERY: 'blob'
    MONTHLY_REPORT_GRANULARITY: defaultGranularity
    MONTHLY_REPORT_RECIPIENT: 'andrew.redman@microsoft.com'
    MONTHLY_REPORT_RUN_ON_STARTUP: 'false'
    MONTHLY_REPORT_SCHEDULE: '0 0 9 1 * *'
    MONTHLY_REPORT_SUBSCRIPTION_ID: subscription().subscriptionId
    PYTHON_ENABLE_INIT_INDEXING: '1'
  }
}

output AZURE_FUNCTION_APP_NAME string = functionApp.name
output FUNCTION_APP_PRINCIPAL_ID string = functionApp.identity.principalId
output DEPLOYMENT_IDENTITY_CLIENT_ID string = deploymentIdentity.properties.clientId
output DEPLOYMENT_IDENTITY_RESOURCE_ID string = deploymentIdentity.id
output SERVICE_API_ENDPOINT_URL string = 'https://${functionApp.properties.defaultHostName}/api'
