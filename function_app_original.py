import html
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import azure.functions as func
import requests
from azure.core.exceptions import ResourceExistsError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

#app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

MANAGEMENT_SCOPE = "https://management.azure.com/.default"
COST_QUERY_API_VERSION = "2025-03-01"
COST_QUERY_CACHE_TTL_SECONDS = 300
DEFAULT_MONTHLY_REPORT_GRANULARITY = "None"


class CostManagementConfigError(ValueError):
    """Raised when the environment is misconfigured."""


@dataclass
class CostManagementApiError(Exception):
    status_code: int
    message: str
    details: Optional[Dict[str, Any]] = None
    retry_after: Optional[str] = None

    def __str__(self) -> str:
        return self.message


_COST_QUERY_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _first_value(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value is None:
            continue

        trimmed = value.strip()
        if trimmed:
            return trimmed

    return None


def _get_int_setting(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        return int(raw_value)
    except ValueError:
        logging.warning("Invalid integer for %s: %s", name, raw_value)
        return default


def _get_bool_setting(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    logging.warning("Invalid boolean for %s: %s", name, raw_value)
    return default


def _build_cache_key(
    subscription_id: str,
    start_date: str,
    end_date: str,
    granularity: str,
) -> str:
    return json.dumps(
        {
            "subscriptionId": subscription_id,
            "startDate": start_date,
            "endDate": end_date,
            "granularity": granularity,
        },
        sort_keys=True,
    )


def _get_cached_cost_query(cache_key: str) -> Optional[Dict[str, Any]]:
    cached_entry = _COST_QUERY_CACHE.get(cache_key)
    if not cached_entry:
        return None

    expires_at, cached_result = cached_entry
    if time.time() >= expires_at:
        _COST_QUERY_CACHE.pop(cache_key, None)
        return None

    return cached_result


def _store_cached_cost_query(cache_key: str, result: Dict[str, Any]) -> None:
    ttl_seconds = _get_int_setting(
        "COST_QUERY_CACHE_TTL_SECONDS", COST_QUERY_CACHE_TTL_SECONDS
    )
    if ttl_seconds <= 0:
        return

    _COST_QUERY_CACHE[cache_key] = (time.time() + ttl_seconds, result)


def _clear_cached_cost_queries() -> None:
    _COST_QUERY_CACHE.clear()


def _json_response(
    payload: Dict[str, Any],
    status_code: int = 200,
    headers: Optional[Dict[str, str]] = None,
) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(payload, indent=2),
        status_code=status_code,
        mimetype="application/json",
        headers=headers,
    )


def _normalize_granularity(granularity: Optional[str]) -> str:
    normalized = (granularity or DEFAULT_MONTHLY_REPORT_GRANULARITY).strip()
    if normalized.lower() == "daily":
        return "Daily"
    if normalized.lower() == "none":
        return "None"

    raise CostManagementConfigError(
        "Unsupported granularity. Supported values are: Daily, None."
    )


def _build_query_definition(
    start_date: str,
    end_date: str,
    granularity: str,
) -> Dict[str, Any]:
    return {
        "type": "Usage",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{start_date}T00:00:00Z",
            "to": f"{end_date}T23:59:59Z",
        },
        "dataset": {
            "aggregation": {
                "totalCost": {
                    "name": "PreTaxCost",
                    "function": "Sum",
                }
            },
            "granularity": granularity,
        },
    }


def _find_column_index(
    column_names: List[Optional[str]], *candidates: str
) -> Optional[int]:
    lowered = [name.lower() if name else None for name in column_names]
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered.index(candidate.lower())

    return None


def _format_usage_date(raw_value: Any) -> str:
    text = str(raw_value)
    if len(text) == 8 and text.isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"

    return text


def _normalize_query_result(
    subscription_id: str,
    start_date: str,
    end_date: str,
    granularity: str,
    response_properties: Dict[str, Any],
) -> Dict[str, Any]:
    columns = response_properties.get("columns", [])
    rows = response_properties.get("rows", [])
    column_names = [column.get("name") for column in columns]

    total_cost_index = _find_column_index(column_names, "totalCost", "PreTaxCost", "Cost")
    usage_date_index = _find_column_index(column_names, "UsageDate")
    currency_index = _find_column_index(column_names, "Currency")

    breakdown: List[Dict[str, Any]] = []
    total_cost = Decimal("0")
    currency: Optional[str] = None

    for row in rows:
        if total_cost_index is None or total_cost_index >= len(row):
            continue

        row_cost = Decimal(str(row[total_cost_index]))
        total_cost += row_cost

        item: Dict[str, Any] = {"totalCost": float(row_cost)}

        if usage_date_index is not None and usage_date_index < len(row):
            item["usageDate"] = _format_usage_date(row[usage_date_index])

        if currency_index is not None and currency_index < len(row):
            currency = currency or str(row[currency_index])
            item["currency"] = str(row[currency_index])

        breakdown.append(item)

    return {
        "subscriptionId": subscription_id,
        "scope": f"/subscriptions/{subscription_id}",
        "periodStart": start_date,
        "periodEnd": end_date,
        "granularity": granularity,
        "currency": currency,
        "totalCost": float(total_cost),
        "rowCount": len(breakdown),
        "columns": column_names,
        "breakdown": breakdown,
    }


def _build_query_url(subscription_id: str) -> str:
    return (
        "https://management.azure.com/subscriptions/"
        f"{subscription_id}/providers/Microsoft.CostManagement/query"
        f"?api-version={COST_QUERY_API_VERSION}"
    )


def _get_access_token() -> str:
    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return credential.get_token(MANAGEMENT_SCOPE).token


def _build_api_error(response: requests.Response) -> CostManagementApiError:
    retry_after = response.headers.get("Retry-After")
    details: Optional[Dict[str, Any]] = None
    message = f"Azure Cost Management query failed with HTTP {response.status_code}."

    try:
        payload = response.json()
        details = payload
        error_message = payload.get("error", {}).get("message")
        if error_message:
            message = error_message
    except ValueError:
        response_text = response.text.strip()
        if response_text:
            message = response_text

    if response.status_code == 403:
        message += (
            " Verify that the calling identity has the Cost Management Reader role "
            "at the subscription scope and that billing visibility settings allow "
            "cost access for the account type."
        )
    elif response.status_code == 429:
        message += " Azure is throttling this request. Retry after the indicated delay."

    return CostManagementApiError(
        status_code=response.status_code,
        message=message,
        details=details,
        retry_after=retry_after,
    )


def _query_cost_for_period(
    subscription_id: str,
    start_date: str,
    end_date: str,
    granularity: str,
) -> Dict[str, Any]:
    normalized_granularity = _normalize_granularity(granularity)
    request_body = _build_query_definition(
        start_date=start_date,
        end_date=end_date,
        granularity=normalized_granularity,
    )
    cache_key = _build_cache_key(
        subscription_id=subscription_id,
        start_date=start_date,
        end_date=end_date,
        granularity=normalized_granularity,
    )
    cached_result = _get_cached_cost_query(cache_key)
    if cached_result is not None:
        return cached_result

    headers = {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
    }

    combined_columns: List[Dict[str, Any]] = []
    combined_rows: List[List[Any]] = []
    next_url: Optional[str] = _build_query_url(subscription_id)

    with requests.Session() as session:
        while next_url:
            response = session.post(next_url, json=request_body, headers=headers, timeout=30)

            if response.status_code == 204:
                result = _normalize_query_result(
                    subscription_id=subscription_id,
                    start_date=start_date,
                    end_date=end_date,
                    granularity=normalized_granularity,
                    response_properties={"columns": [], "rows": []},
                )
                _store_cached_cost_query(cache_key, result)
                return result

            if response.status_code >= 400:
                raise _build_api_error(response)

            payload = response.json()
            properties = payload.get("properties", {})

            if not combined_columns:
                combined_columns = properties.get("columns", [])

            combined_rows.extend(properties.get("rows", []))
            next_url = properties.get("nextLink")

    result = _normalize_query_result(
        subscription_id=subscription_id,
        start_date=start_date,
        end_date=end_date,
        granularity=normalized_granularity,
        response_properties={"columns": combined_columns, "rows": combined_rows},
    )
    _store_cached_cost_query(cache_key, result)
    return result


def _render_html_report(result: Dict[str, Any]) -> str:
    rows = result.get("breakdown", [])
    table_rows = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(item.get('usageDate', '')))}</td>"
            f"<td>{html.escape(str(item.get('currency', result.get('currency', ''))))}</td>"
            f"<td>{html.escape(str(item.get('totalCost', '')))}</td>"
            "</tr>"
        )
        for item in rows
    )

    if not table_rows:
        table_rows = (
            '<tr><td colspan="3">No rows returned for the selected billing period.</td></tr>'
        )

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <title>Azure Cost Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #1f2937; }}
    h1 {{ margin-bottom: 8px; }}
    .summary {{ margin-bottom: 20px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: left; }}
    th {{ background: #f3f4f6; }}
    code {{ background: #f3f4f6; padding: 2px 4px; }}
  </style>
</head>
<body>
  <h1>Azure Cost Report</h1>
  <div class=\"summary\">
    <p><strong>Subscription:</strong> <code>{html.escape(result['subscriptionId'])}</code></p>
    <p><strong>Period:</strong> {html.escape(result['periodStart'])} to {html.escape(result['periodEnd'])}</p>
    <p><strong>Granularity:</strong> {html.escape(result['granularity'])}</p>
    <p><strong>Total Cost:</strong> {html.escape(str(result['totalCost']))} {html.escape(str(result.get('currency') or ''))}</p>
  </div>
  <table>
    <thead>
      <tr>
        <th>Usage Date</th>
        <th>Currency</th>
        <th>Total Cost</th>
      </tr>
    </thead>
    <tbody>
      {table_rows}
    </tbody>
  </table>
</body>
</html>"""


def _resolve_previous_month_range(
    reference_date: Optional[date] = None,
) -> Tuple[str, str]:
    current_date = reference_date or datetime.now(timezone.utc).date()
    first_day_of_current_month = current_date.replace(day=1)
    last_day_of_previous_month = first_day_of_current_month - timedelta(days=1)
    first_day_of_previous_month = last_day_of_previous_month.replace(day=1)
    return (
        first_day_of_previous_month.isoformat(),
        last_day_of_previous_month.isoformat(),
    )


def _build_report_run_id() -> str:
    return uuid4().hex[:8]


def _build_monthly_report_filename(start_date: str, run_id: Optional[str] = None) -> str:
    report_run_id = run_id or _build_report_run_id()
    return f"cost-report-{start_date[:7]}-{report_run_id}.html"


def _get_blob_service_client() -> BlobServiceClient:
    storage_connection_string = os.getenv("AzureWebJobsStorage")
    if storage_connection_string:
        logging.info("Using AzureWebJobsStorage connection string for blob delivery.")
        return BlobServiceClient.from_connection_string(storage_connection_string)

    storage_account_name = _first_value(os.getenv("AzureWebJobsStorage__accountName"))
    if not storage_account_name:
        raise CostManagementConfigError(
            "AzureWebJobsStorage or AzureWebJobsStorage__accountName must be configured "
            "for blob delivery."
        )

    managed_identity_client_id = _first_value(os.getenv("AzureWebJobsStorage__clientId"))
    logging.info(
        "Using managed identity blob delivery for storage account %s with client id %s.",
        storage_account_name,
        managed_identity_client_id or "<default>",
    )
    credential = DefaultAzureCredential(
        exclude_interactive_browser_credential=True,
        managed_identity_client_id=managed_identity_client_id,
    )
    return BlobServiceClient(
        account_url=f"https://{storage_account_name}.blob.core.windows.net",
        credential=credential,
    )


def _upload_report_to_blob(
    container_name: str,
    blob_name: str,
    report_html: str,
) -> None:
    logging.info(
        "Uploading monthly report blob %s to container %s.",
        blob_name,
        container_name,
    )
    blob_service_client = _get_blob_service_client()
    container_client = blob_service_client.get_container_client(container_name)
    try:
        container_client.create_container()
        logging.info("Created blob container %s for monthly reports.", container_name)
    except ResourceExistsError:
        logging.info("Blob container %s already exists.", container_name)

    container_client.upload_blob(
        name=blob_name,
        data=report_html.encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="text/html; charset=utf-8"),
    )
    logging.info("Uploaded monthly report blob %s successfully.", blob_name)




def _run_monthly_report() -> Dict[str, Any]:
    subscription_id = _first_value(os.getenv("MONTHLY_REPORT_SUBSCRIPTION_ID"))
    if not subscription_id:
        raise CostManagementConfigError(
            "MONTHLY_REPORT_SUBSCRIPTION_ID must be configured for the monthly report."
        )

    granularity = _normalize_granularity(
        _first_value(
            os.getenv("MONTHLY_REPORT_GRANULARITY"),
            DEFAULT_MONTHLY_REPORT_GRANULARITY,
        )
    )
    start_date, end_date = _resolve_previous_month_range()
    logging.info(
        "Preparing monthly report for subscription %s covering %s to %s with granularity %s.",
        subscription_id,
        start_date,
        end_date,
        granularity,
    )
    result = _query_cost_for_period(
        subscription_id=subscription_id,
        start_date=start_date,
        end_date=end_date,
        granularity=granularity,
    )
    report_html = _render_html_report(result)
    report_filename = _build_monthly_report_filename(start_date)
    container_name = _first_value(
        os.getenv("MONTHLY_REPORT_BLOB_CONTAINER"),
        "monthly-cost-reports",
    )
    _upload_report_to_blob(
        container_name=container_name,
        blob_name=report_filename,
        report_html=report_html,
    )
    logging.info(
        "Uploaded monthly cost report %s to blob container %s.",
        report_filename,
        container_name,
    )
    return {
        "status": "ok",
        "delivery": "blob",
        "container": container_name,
        "reportFilename": report_filename,
        "startDate": start_date,
        "endDate": end_date,
        "subscriptionId": subscription_id,
    }



@app.timer_trigger(
    schedule="%MONTHLY_REPORT_SCHEDULE%",
    arg_name="monthly_timer",
    run_on_startup=_get_bool_setting("MONTHLY_REPORT_RUN_ON_STARTUP", False),
    use_monitor=True,
)
def monthly_cost_report(monthly_timer: func.TimerRequest) -> None:
    logging.info(
        "monthly_cost_report triggered. past_due=%s",
        monthly_timer.past_due,
    )
    if monthly_timer.past_due:
        logging.warning("The monthly cost report timer trigger is running late.")

    try:
        _run_monthly_report()
    except Exception:
        logging.exception("monthly_cost_report failed.")
        raise


@app.route(route="reports/monthly/run", methods=["GET", "POST"])
def run_monthly_report(req: func.HttpRequest) -> func.HttpResponse:
    try:
        return _json_response(_run_monthly_report())
    except CostManagementConfigError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    except CostManagementApiError as exc:
        response_payload: Dict[str, Any] = {
            "error": str(exc),
            "statusCode": exc.status_code,
        }
        if exc.retry_after:
            response_payload["retryAfter"] = exc.retry_after
        if exc.details:
            response_payload["details"] = exc.details

        response_headers: Optional[Dict[str, str]] = None
        if exc.retry_after:
            response_headers = {"Retry-After": exc.retry_after}
        return _json_response(
            response_payload,
            status_code=exc.status_code,
            headers=response_headers,
        )
    except Exception:
        logging.exception("run_monthly_report failed.")
        return _json_response(
            {
                "error": (
                    "Unexpected error while running the monthly report. "
                    "Check Function logs for details."
                )
            },
            status_code=500,
        )


@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json_response({"status": "ok"})
