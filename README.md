# Azure Cost Management Monthly Report

A Python Azure Function App that queries the Azure Cost Management API across all accessible subscriptions and emails a monthly cost report with a CSV attachment.

**Triggers:**
- `MonthlyReport` — timer, runs on the 1st of each month
- `RunEmailCostReport` — HTTP GET/POST for on-demand runs
- `health` — health check endpoint

Uses `DefaultAzureCredential` (managed identity in Azure, CLI auth locally).

## Prerequisites

### RBAC

The calling identity needs `Cost Management Reader` on each subscription to include in the report. Repeat per subscription:

```bash
az role assignment create \
  --assignee <managed-identity-object-id> \
  --role "Cost Management Reader" \
  --scope /subscriptions/<subscription-id>
```

### Service principal (email report)

```bash
# Create app + service principal
az ad app create --display-name "<name>" --query "{appId:appId}" -o json
az ad sp create --id <app-id>

# Create a client secret
az ad app credential reset --id <app-id> \
  --end-date "$(date -v+6m -u +"%Y-%m-%dT%H:%M:%SZ")" \
  --query "{clientId:appId,clientSecret:password,tenantId:tenant}" -o json
```

> Save `clientSecret` immediately — it cannot be retrieved later.

Assign `Cost Management Reader` on each subscription (see RBAC above), then set these Function App settings: `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`.

### Billing visibility

Some `403` errors are billing-policy issues, not code:
- **MCA**: enable Azure charges access
- **EA**: enable `AO view charges`

## Local setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp local.settings.sample.json local.settings.json
```

Set in `local.settings.json`: `MONTHLY_REPORT_SUBSCRIPTION_ID`, `languageWorkers__python__defaultExecutablePath`

```bash
azurite &   # storage emulator
az login
func start
```

Test locally:
```bash
curl http://localhost:7071/api/health
curl -X POST http://localhost:7071/api/reports/email/run
```

## App settings reference

| Setting | Default | Description |
|---------|---------|-------------|
| `ACS_CONNECTION_STRING` | — | Azure Communication Services connection string |
| `ACS_SENDER_EMAIL` | — | From address |
| `ACS_RECIPIENT_EMAIL` | — | To address(es), comma or semicolon separated |
| `TENANT_ID` | — | Service principal tenant |
| `CLIENT_ID` | — | Service principal client ID |
| `CLIENT_SECRET` | — | Service principal client secret |

## Deployment

### GitHub Actions (CI/CD)

The workflow runs on every push to `main` and can also be triggered manually.

**Required GitHub secrets** (Settings → Secrets and variables → Actions → Secrets):

| Secret | Description |
|--------|-------------|
| `AZURE_CLIENT_ID` | Federated identity client ID for OIDC login |
| `AZURE_TENANT_ID` | Azure tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Deployment subscription ID |
| `COST_API_TENANT_ID` | Service principal tenant |
| `COST_API_CLIENT_ID` | Service principal client ID |
| `COST_API_CLIENT_SECRET` | Service principal client secret |

**Required GitHub variables** (Settings → Secrets and variables → Actions → Variables):

| Variable | Example |
|----------|---------|
| `AZURE_RESOURCE_GROUP` | `costdemo-red-rg` |
| `AZURE_LOCATION` | `eastus2` |

### Manual deployment

```bash
az login && azd auth login
export AZD_ENV_NAME=costdemo
export AZURE_SUBSCRIPTION_ID=<id>
export AZURE_RESOURCE_GROUP=costdemo-red-rg
export AZURE_LOCATION=eastus2

./scripts/provision-infra.sh
./scripts/package-function-code.sh
./scripts/deploy-function-code.sh
./scripts/configure-function-settings.sh
./scripts/assign-cost-reader.sh
./scripts/smoke-test.sh
```

### Post-deploy verification

```bash
eval "$(azd env get-values)"
BASE_URL="https://${AZURE_FUNCTION_APP_NAME}.azurewebsites.net/api"

HEALTH_KEY=$(az functionapp function keys list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --function-name health --query default -o tsv)

curl "${BASE_URL}/health?code=${HEALTH_KEY}"
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `403 Forbidden` | Check `Cost Management Reader` assignment, subscription ID, and billing visibility settings. Wait for RBAC propagation if recently assigned. |
| `429 Too Many Requests` | Reduce trigger frequency; keep `MONTHLY_REPORT_GRANULARITY=None`; honor `Retry-After`. |
| Empty cost data | Confirm billable usage exists for the period and the account type supports Cost Management. |
| Zero functions after deploy | Verify `AzureWebJobsStorage`, `AzureWebJobsFeatureFlags=EnableWorkerIndexing`, `PYTHON_ENABLE_INIT_INDEXING=1`, and that the zip was built on Linux. |

