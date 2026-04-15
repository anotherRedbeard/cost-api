# Azure Cost Management Monthly Report Demo

This sample is intentionally small. It shows one Azure Cost Management query, one HTML report, and two ways to trigger it:

- `monthly_cost_report` runs on a timer once a month
- `run_monthly_report` runs the same flow on demand with a browser `GET` or API `POST`
- `health` gives you a simple health check

The Function App uses `DefaultAzureCredential`, managed identity in Azure, and the subscription-scoped Cost Management Query API.

## What the sample does

Each run:

- queries the previous calendar month for one configured subscription
- aggregates `PreTaxCost`
- renders a simple HTML report
- writes the report to the `monthly-cost-reports` blob container by default

This keeps the demo focused on the core Cost API pattern instead of supporting lots of request shapes.

## Important prerequisites

### RBAC

The calling identity needs at least:

- `Cost Management Reader` on the target subscription

Example:

```bash
az role assignment create \
  --assignee <managed-identity-object-id> \
  --role "Cost Management Reader" \
  --scope /subscriptions/<subscription-id>
```

### Service principal setup (for email cost report)

If you need a service principal with a client secret (e.g. for the multi-subscription email report), create one with the following commands.

Create the app registration:

```bash
az ad app create --display-name "<app-name>" --query "{appId: appId, objectId: id}" -o json
```

Create the service principal:

```bash
az ad sp create --id <app-id> --query "{servicePrincipalId: id, appId: appId}" -o json
```

Generate a client secret (6-month expiration):

```bash
end_date=$(date -v+6m -u +"%Y-%m-%dT%H:%M:%SZ")  # macOS
# end_date=$(date -d "+6 months" -u +"%Y-%m-%dT%H:%M:%SZ")  # Linux

az ad app credential reset \
  --id <app-id> \
  --end-date "$end_date" \
  --query "{clientId: appId, clientSecret: password, tenantId: tenant}" \
  -o json
```

> **Save the `clientSecret` immediately** — it cannot be retrieved later.

Assign Cost Management Reader on each subscription:

```bash
az role assignment create \
  --assignee <app-id> \
  --role "Cost Management Reader" \
  --scope /subscriptions/<subscription-id>
```

> **Cost Management Reader** is the only role needed. It grants both subscription visibility (so the subscription appears in the list API) and read access to the Cost Management Query API. Repeat the command for each subscription you want in the report.

Set these values in the Function App configuration:

- `TENANT_ID`
- `CLIENT_ID`
- `CLIENT_SECRET`

### Billing visibility settings

Some `403` problems are caused by billing settings rather than code.

- For Microsoft Customer Agreement accounts, Azure charges access must be enabled where applicable.
- For Enterprise Agreement accounts, `AO view charges` or related visibility settings must be enabled where applicable.

## Local setup

Create and activate a Python 3.11 virtual environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Copy the sample settings:

```bash
cp local.settings.sample.json local.settings.json
```

Set these values in `local.settings.json`:

- `MONTHLY_REPORT_SUBSCRIPTION_ID`
- `languageWorkers__python__defaultExecutablePath`

Start Azurite in another terminal:

```bash
azurite
```

Authenticate locally:

```bash
az login
```

Start the Functions host:

```bash
func start
```

## Local testing

Health check:

```bash
curl "http://localhost:7071/api/health"
```

Run the monthly report manually with `POST`:

```bash
curl -X POST "http://localhost:7071/api/reports/monthly/run"
```

Or just open this in a browser:

```text
http://localhost:7071/api/reports/monthly/run
```

A successful run returns JSON like:

```json
{
  "status": "ok",
  "delivery": "blob",
  "container": "monthly-cost-reports",
  "reportFilename": "cost-report-2026-03-<run-id>.html"
}
```

Each run now appends a short unique ID to the filename, so repeated manual or timer
runs create new blobs instead of overwriting the previous report.

## Monthly report settings

The app uses these settings:

- `MONTHLY_REPORT_SCHEDULE=0 0 9 1 * *`
- `MONTHLY_REPORT_RUN_ON_STARTUP=false`
- `MONTHLY_REPORT_SUBSCRIPTION_ID=<subscription-id>`
- `MONTHLY_REPORT_GRANULARITY=None`
- `MONTHLY_REPORT_BLOB_CONTAINER=monthly-cost-reports`


## Azure deployment workflow

This repo uses a split deployment model:

1. `./scripts/provision-infra.sh`
2. `./scripts/package-function-code.sh`
3. `./scripts/deploy-function-code.sh`
4. `./scripts/assign-cost-reader.sh`
5. `./scripts/smoke-test.sh`

The workflow targets the shared `costdemo` `azd` environment so repeated runs update the existing deployment instead of creating duplicate Azure resources.

## Manual deployment

Sign in and select the Azure subscription for deployment:

```bash
az login
az account set --subscription <deployment-subscription-id>
azd auth login
```

Export deployment settings:

```bash
export AZD_ENV_NAME=costdemo
export AZURE_SUBSCRIPTION_ID=<deployment-subscription-id>
export AZURE_RESOURCE_GROUP=costdemo-red-rg
export AZURE_LOCATION=eastus2
```

Provision infrastructure:

```bash
./scripts/provision-infra.sh
```

Package the Function App on Linux:

```bash
./scripts/package-function-code.sh
```

Deploy the Function App code:

```bash
./scripts/deploy-function-code.sh
```

Grant Cost Management Reader:

```bash
./scripts/assign-cost-reader.sh
```

Smoke test the deployed app:

```bash
./scripts/smoke-test.sh
```

## Manual post-deploy testing

Get function keys:

```bash
eval "$(azd env get-values)"

HEALTH_KEY=$(az functionapp function keys list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --function-name health \
  --query default -o tsv)

REPORT_KEY=$(az functionapp function keys list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --function-name run_monthly_report \
  --query default -o tsv)

BASE_URL="https://${AZURE_FUNCTION_APP_NAME}.azurewebsites.net/api"
```

Health:

```bash
curl "${BASE_URL}/health?code=${HEALTH_KEY}"
```

Run the report manually:

```bash
curl "${BASE_URL}/reports/monthly/run?code=${REPORT_KEY}"
```

Or open this in a browser:

```text
${BASE_URL}/reports/monthly/run?code=${REPORT_KEY}
```

## Deployment verification checklist

After deployment, verify:

- `az functionapp function list` shows `health`, `monthly_cost_report`, and `run_monthly_report`
- the Function App is running
- the managed identity has `Cost Management Reader`
- billing visibility settings allow cost access
- `health` returns `200`
- the manual monthly report route returns `200`
- a report blob appears in `monthly-cost-reports`

## Troubleshooting

### `403 Forbidden`

Check all of the following:

- the identity has `Cost Management Reader`
- the configured `MONTHLY_REPORT_SUBSCRIPTION_ID` is correct
- the billing model allows cost visibility for the caller
- if you just created the role assignment, wait briefly for RBAC propagation and retry

### `429 Too Many Requests`

- reduce how often you run the manual trigger
- keep `MONTHLY_REPORT_GRANULARITY=None` unless you truly need daily rows
- honor the `Retry-After` header

### Empty data

- confirm the subscription has billable usage in the selected month
- verify the account type and scope support Cost Management data access

### Function App shows zero functions after deployment

If Azure shows an empty function list and the deployed app returns `404`, check these items in order:

- confirm `AzureWebJobsStorage` is present and valid
- confirm `AzureWebJobsFeatureFlags=EnableWorkerIndexing`
- confirm `PYTHON_ENABLE_INIT_INDEXING=1`
- confirm your zip includes `host.json` at the root
- confirm the package was built on Linux so `.python_packages` matches Azure Functions Linux
- rerun `./scripts/deploy-function-code.sh` and verify trigger sync succeeds
