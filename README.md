# Azure Cost Management Function App Demo

This sample shows the recommended way for a Python Azure Function App to query Azure Cost Management data by subscription, while keeping the implementation intentionally simple:

- Azure Functions Python v2 programming model
- one main file: `function_app.py`
- `DefaultAzureCredential` for local development and managed identity in Azure
- Azure Cost Management Query REST API at subscription scope
- least-privilege RBAC with `Cost Management Reader`

## What the sample does

The HTTP-triggered function calls:

`POST https://management.azure.com/subscriptions/{subscriptionId}/providers/Microsoft.CostManagement/query?api-version=2025-03-01`

It sends a query that aggregates `PreTaxCost` and can return either:

- a downloadable JSON report
- a downloadable HTML report

The point of the sample is to keep the code easy to follow so you can focus on the Azure auth, RBAC, deployment, and query flow.

## Why this version is useful

- Authentication is secretless for the Cost API path.
- The same code works locally through Azure CLI sign-in and in Azure through managed identity.
- The sample keeps the logic in one file, which makes it easier to compare with a customer proof-of-concept.
- Errors such as `403` and `429` are surfaced clearly instead of being swallowed.

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

### Billing visibility settings

Some customer issues are caused by billing settings rather than code.

- For Microsoft Customer Agreement accounts, Azure charges access must be enabled where applicable.
- For Enterprise Agreement accounts, `AO view charges` or related visibility settings must be enabled where applicable.

If the code has RBAC but still receives `403`, check billing visibility settings next.

## Local prerequisites

- Python 3.11 recommended for Azure deployment compatibility
- Azure Functions Core Tools v4
- Azure CLI
- Azurite storage emulator

Install Azurite if needed:

```bash
npm install -g azurite
```

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

Copy local settings if you want a clean local file:

```bash
cp local.settings.sample.json local.settings.json
```

Set `languageWorkers__python__defaultExecutablePath` to your local virtual environment's Python executable so Core Tools uses the same interpreter and packages as your shell. The checked-in sample file uses a placeholder path for that value.

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

## Invoke the function locally

Health check:

```bash
curl http://localhost:7071/api/health
```

JSON report:

```bash
curl -OJ "http://localhost:7071/api/cost/subscription?subscriptionId=<subscription-id>&format=json&granularity=None"
```

HTML report:

```bash
curl -OJ "http://localhost:7071/api/cost/subscription?subscriptionId=<subscription-id>&format=html&granularity=None"
```

Custom range JSON report:

```bash
curl -OJ "http://localhost:7071/api/cost/subscription?subscriptionId=<subscription-id>&from=2026-03-01&to=2026-03-05&granularity=None&format=json"
```

POST example for HTML:

```bash
curl -X POST "http://localhost:7071/api/cost/subscription" \
  -H "Content-Type: application/json" \
  -d '{
    "subscriptionId": "<subscription-id>",
    "timeframe": "MonthToDate",
    "granularity": "None",
    "format": "html"
  }'
```

## Azure deployment workflow

This repo now uses a split deployment model:

- `azd` provisions infrastructure only
- a separate code deployment step publishes the Function App package
- a separate RBAC step grants `Cost Management Reader`
- a smoke test step validates the live deployment

This split is intentional. In live testing, `azd up` reliably created the Azure resources, but the `azd` service deployment path repeatedly left the Function App with zero indexed functions. The current recommendation for this sample is:

- use `azd provision`
- package the Function App on Linux
- deploy the zip to a Flex Consumption Function App configured with managed-identity-backed deployment storage

### What is in the repo for Azure

- `azure.yaml` for infra-only `azd` usage
- `infra/main.bicep`
- `scripts/provision-infra.sh`
- `scripts/package-function-code.sh`
- `scripts/deploy-function-code.sh`
- `scripts/assign-cost-reader.sh`
- `scripts/smoke-test.sh`
- `.github/workflows/deploy-azure.yml`

## Manual end-to-end deployment

### 1. Sign in and select the Azure subscription for deployment

```bash
az login
az account set --subscription <deployment-subscription-id>
azd auth login
```

Verify the active subscription:

```bash
az account show --query '{name:name,id:id}' -o json
```

### 2. Export the deployment settings

```bash
export AZD_ENV_NAME=costdemo
export AZURE_SUBSCRIPTION_ID=<deployment-subscription-id>
export AZURE_LOCATION=eastus2
```

Optional defaults:

```bash
export COST_QUERY_TIMEFRAME=MonthToDate
export COST_QUERY_GRANULARITY=None
```

Optional if you want the app to fall back to a default subscription when a request does not provide `subscriptionId`:

```bash
export COST_SUBSCRIPTION_ID=<optional-default-subscription-id>
```

### 3. Provision infrastructure with `azd`

```bash
./scripts/provision-infra.sh
```

This provisions:

- a Linux Function App running Python 3.11
- a Flex Consumption hosting plan
- a storage account
- Application Insights
- a Log Analytics workspace
- a system-assigned managed identity on the Function App for Cost Management access
- a user-assigned managed identity for Function host storage and deployment package access

### 4. Package the Function App code on Linux

Run this step on Linux, GitHub Actions, WSL, or a Linux container so the packaged dependencies match Azure Functions Linux:

```bash
./scripts/package-function-code.sh
```

This creates:

```bash
dist/functionapp.zip
```

### 5. Deploy the Function App code

```bash
./scripts/deploy-function-code.sh
```

This performs the code deployment to the Flex Consumption app, calls `syncfunctiontriggers`, and waits for Azure to report the registered functions.

### 6. Grant the Function App permission to read Cost Management data

```bash
./scripts/assign-cost-reader.sh
```

By default, this grants `Cost Management Reader` on the deployment subscription. To grant access at a broader or different scope, set `COST_ROLE_SCOPE` first. For example:

```bash
export COST_ROLE_SCOPE=/providers/Microsoft.Management/managementGroups/<management-group-id>
./scripts/assign-cost-reader.sh
```

### 7. Smoke test the deployed app

```bash
./scripts/smoke-test.sh
```

That script validates:

- `health` returns `200`
- the cost endpoint returns a controlled `400` when `subscriptionId` is omitted

If you also want the smoke test to run a real cost query, set a target just for the test:

```bash
export SMOKE_TEST_SUBSCRIPTION_ID=<subscription-id>
./scripts/smoke-test.sh
```

## GitHub Actions pipeline

The repo includes `.github/workflows/deploy-azure.yml` for a full on-demand deployment pipeline.

### Required GitHub configuration

Repository variable:

- `AZURE_SUBSCRIPTION_ID`

Repository secrets:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`

The workflow uses `azure/login` with OpenID Connect, so the Azure application behind `AZURE_CLIENT_ID` must have a federated credential configured for your GitHub repository.

### Running the workflow

From GitHub Actions, run `Deploy Azure Cost API` and provide:

- `azd_environment`
- `azure_location`

The workflow then runs:

1. `./scripts/provision-infra.sh`
2. `./scripts/package-function-code.sh`
3. `./scripts/deploy-function-code.sh`
4. `./scripts/assign-cost-reader.sh`
5. `./scripts/smoke-test.sh`

## Billing visibility settings

RBAC alone is not always enough.

- For MCA accounts, make sure Azure charges visibility is enabled where applicable.
- For EA accounts, make sure charge visibility settings such as `AO view charges` are enabled where applicable.

If deployment succeeds but every API call returns `403`, check this immediately after RBAC.

## Run targeted tests after deployment

If you want to test manually instead of using `scripts/smoke-test.sh`, use these commands.

### 1. Get the function keys

```bash
eval "$(azd env get-values)"

HEALTH_KEY=$(az functionapp function keys list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --function-name health \
  --query default -o tsv)

COST_KEY=$(az functionapp function keys list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --function-name subscription_cost \
  --query default -o tsv)
```

Base URL:

```bash
BASE_URL="https://${AZURE_FUNCTION_APP_NAME}.azurewebsites.net/api"
```

### 2. Smoke test the health endpoint

```bash
curl "${BASE_URL}/health?code=${HEALTH_KEY}"
```

### 3. Test month-to-date cost

```bash
curl "${BASE_URL}/cost/subscription?code=${COST_KEY}&subscriptionId=<target-cost-subscription-id>&timeframe=MonthToDate&granularity=None"
```

### 4. Test a custom range

Custom ranges are often safer than built-in relative timeframe enums.

```bash
curl "${BASE_URL}/cost/subscription?code=${COST_KEY}&subscriptionId=<target-cost-subscription-id>&from=2026-02-01&to=2026-02-28&granularity=None"
```

### 5. Test an expected validation failure

```bash
curl "${BASE_URL}/cost/subscription?code=${COST_KEY}"
```

## Suggested deployment verification checklist

After deployment, verify all of the following:

- `./scripts/provision-infra.sh` or `azd provision` completed successfully
- the Function App exists and is running
- `az functionapp function list` shows `health` and `subscription_cost`
- the system-assigned identity exists
- the identity has `Cost Management Reader`
- billing visibility settings allow cost access
- `health` returns `200`
- `MonthToDate` query returns `200`
- a custom date range query returns `200`

## Notes from live testing

- `MonthToDate` worked successfully in live testing.
- A custom date range also worked successfully.
- `TheLastMonth` returned a `400` for the tested subscription, so custom date ranges are the safer recommendation when you need previous-month data.

## Before production use, review

- host storage hardening
- network restrictions
- alerting
- retry strategy and caching for repeated cost queries
- whether to make `health` anonymous instead of function-key protected
- whether to move host storage to identity-based configuration

## Troubleshooting

### `403 Forbidden`

Check all of the following:

- the identity has `Cost Management Reader`
- the subscription ID is correct
- the billing model allows cost visibility for the caller
- the caller is querying a supported scope
- if you just created the role assignment, wait briefly for RBAC propagation and retry

### `429 Too Many Requests`

- reduce polling frequency
- cache responses when possible
- honor the `Retry-After` header

### Empty data

- try a broader timeframe such as `TheLastMonth`
- confirm the subscription actually has billable usage in the selected period
- verify the account type and scope support Cost Management data access

### Built-in timeframe returns `400`

Some scopes or account combinations may reject built-in relative timeframes such as `TheLastMonth`.

If that happens, retry with explicit dates:

```bash
curl "${BASE_URL}/cost/subscription?code=${COST_KEY}&subscriptionId=<target-cost-subscription-id>&from=2026-02-01&to=2026-02-28&granularity=None"
```

### Function App shows zero functions after deployment

If Azure shows an empty function list and the deployed app returns `404` for `/api/health`, check these items in order:

- confirm `AzureWebJobsStorage` is present and valid
- confirm `AzureWebJobsFeatureFlags=EnableWorkerIndexing`
- confirm `PYTHON_ENABLE_INIT_INDEXING=1`
- confirm your code package includes `host.json` at the zip root
- confirm the package was built on Linux so `.python_packages` matches Azure Functions Linux
- rerun `./scripts/deploy-function-code.sh` and verify trigger sync succeeds

For this sample, the most reliable model is `azd` for infrastructure plus a separate zip deployment for code.

### Policy blocks shared access keys

This sample now targets Flex Consumption with managed-identity-backed storage configuration so it can work in environments that prohibit shared access keys on the Function App storage account.

If your organization blocks shared keys, rerun the infrastructure step after pulling the Flex Consumption changes so the Function App and deployment storage are recreated with the new model.
