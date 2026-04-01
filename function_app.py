import html
import json
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import azure.functions as func
import requests
from azure.core.exceptions import ResourceExistsError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

MANAGEMENT_SCOPE = "https://management.azure.com/.default"
COST_QUERY_API_VERSION = "2025-03-01"
COST_QUERY_CACHE_TTL_SECONDS = 300
DEFAULT_MONTHLY_REPORT_RECIPIENT = "andrew.redman@microsoft.com"
ALLOWED_TIMEFRAMES = {
    "BillingMonthToDate": "BillingMonthToDate",
    "Custom": "Custom",
    "MonthToDate": "MonthToDate",
    "TheLastBillingMonth": "TheLastBillingMonth",
    "TheLastMonth": "TheLastMonth",
    "TheLastWeek": "TheLastWeek",
    "TheLastYear": "TheLastYear",
    "WeekToDate": "WeekToDate",
}


class CostManagementConfigError(ValueError):
    """Raised when the request or environment is misconfigured."""


@dataclass
class CostManagementApiError(Exception):
    status_code: int
    message: str
    details: Optional[Dict[str, Any]] = None
    retry_after: Optional[str] = None

    def __str__(self) -> str:
        return self.message


_COST_QUERY_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _load_request_payload(req: func.HttpRequest) -> Dict[str, Any]:
    if not req.get_body():
        return {}

    try:
        payload = req.get_json()
    except ValueError as exc:
        raise CostManagementConfigError(
            "Request body must be valid JSON when a body is supplied."
        ) from exc

    if not isinstance(payload, dict):
        raise CostManagementConfigError("Request body must be a JSON object.")

    return payload


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
    timeframe: str,
    granularity: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> str:
    return json.dumps(
        {
            "subscriptionId": subscription_id,
            "timeframe": timeframe,
            "granularity": granularity,
            "startDate": start_date,
            "endDate": end_date,
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


def _file_response(
    body: str,
    mimetype: str,
    filename: str,
    status_code: int = 200,
    disposition: str = "attachment",
) -> func.HttpResponse:
    return func.HttpResponse(
        body=body,
        status_code=status_code,
        mimetype=mimetype,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


def _normalize_granularity(granularity: str) -> str:
    normalized = (granularity or "Daily").strip()
    if normalized.lower() == "daily":
        return "Daily"
    if normalized.lower() == "none":
        return "None"

    raise CostManagementConfigError(
        "Unsupported granularity. Supported values are: Daily, None."
    )


def _parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CostManagementConfigError(
            f"{field_name} must use ISO format YYYY-MM-DD."
        ) from exc


def _resolve_time_period(
    timeframe: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> Tuple[str, Optional[Dict[str, str]]]:
    normalized_timeframe = (timeframe or "MonthToDate").strip()

    if start_date or end_date:
        if not start_date or not end_date:
            raise CostManagementConfigError(
                "Both startDate and endDate are required when querying a custom range."
            )

        start = _parse_iso_date(start_date, "startDate")
        end = _parse_iso_date(end_date, "endDate")
        if start > end:
            raise CostManagementConfigError("startDate cannot be after endDate.")

        return "Custom", {
            "from": f"{start.isoformat()}T00:00:00Z",
            "to": f"{end.isoformat()}T23:59:59Z",
        }

    if normalized_timeframe not in ALLOWED_TIMEFRAMES:
        raise CostManagementConfigError(
            "Unsupported timeframe. Supported values are: "
            + ", ".join(sorted(ALLOWED_TIMEFRAMES))
        )

    return ALLOWED_TIMEFRAMES[normalized_timeframe], None


def _build_query_definition(
    timeframe: str,
    granularity: str,
    time_period: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "type": "Usage",
        "timeframe": timeframe,
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

    if time_period:
        body["timePeriod"] = time_period

    return body


def _find_column_index(column_names: List[Optional[str]], *candidates: str) -> Optional[int]:
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
    timeframe: str,
    granularity: str,
    time_period: Optional[Dict[str, str]],
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
        "timeframe": timeframe,
        "granularity": granularity,
        "timePeriod": time_period,
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
    from azure.identity import DefaultAzureCredential

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


def _query_subscription_cost(
    subscription_id: str,
    timeframe: str,
    granularity: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> Dict[str, Any]:
    query_timeframe, time_period = _resolve_time_period(
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
    )
    normalized_granularity = _normalize_granularity(granularity)
    request_body = _build_query_definition(
        timeframe=query_timeframe,
        granularity=normalized_granularity,
        time_period=time_period,
    )
    cache_key = _build_cache_key(
        subscription_id=subscription_id,
        timeframe=query_timeframe,
        granularity=normalized_granularity,
        start_date=start_date,
        end_date=end_date,
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
                    timeframe=query_timeframe,
                    granularity=normalized_granularity,
                    time_period=time_period,
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
        timeframe=query_timeframe,
        granularity=normalized_granularity,
        time_period=time_period,
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
            "<tr><td colspan=\"3\">No rows returned for the selected scope and time window.</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
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
  <div class="summary">
    <p><strong>Subscription:</strong> <code>{html.escape(result["subscriptionId"])}</code></p>
    <p><strong>Timeframe:</strong> {html.escape(result["timeframe"])}</p>
    <p><strong>Granularity:</strong> {html.escape(result["granularity"])}</p>
    <p><strong>Total Cost:</strong> {html.escape(str(result["totalCost"]))} {html.escape(str(result.get("currency") or ""))}</p>
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


def _build_monthly_report_filename(start_date: str) -> str:
    return f"cost-report-{start_date[:7]}.html"


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


def _send_email_attachment(
    recipient: str,
    subject: str,
    attachment_name: str,
    attachment_body: str,
) -> None:
    smtp_host = _first_value(os.getenv("SMTP_HOST"))
    smtp_from = _first_value(os.getenv("SMTP_FROM"), os.getenv("SMTP_USERNAME"))
    smtp_username = _first_value(os.getenv("SMTP_USERNAME"))
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_port = _get_int_setting("SMTP_PORT", 587)
    smtp_starttls = _get_bool_setting("SMTP_STARTTLS", smtp_port != 465)

    if not smtp_host:
        raise CostManagementConfigError("SMTP_HOST must be configured for monthly email.")
    if not smtp_from:
        raise CostManagementConfigError(
            "SMTP_FROM or SMTP_USERNAME must be configured for monthly email."
        )
    if smtp_password and not smtp_username:
        raise CostManagementConfigError(
            "SMTP_USERNAME is required when SMTP_PASSWORD is configured."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = recipient
    message.set_content(
        "Attached is your monthly Azure Cost Management report."
    )
    message.add_attachment(
        attachment_body.encode("utf-8"),
        maintype="text",
        subtype="html",
        filename=attachment_name,
    )

    ssl_context = ssl.create_default_context()

    if smtp_port == 465:
        with smtplib.SMTP_SSL(
            smtp_host,
            smtp_port,
            timeout=30,
            context=ssl_context,
        ) as smtp_client:
            if smtp_username and smtp_password:
                smtp_client.login(smtp_username, smtp_password)
            smtp_client.send_message(message)
        return

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp_client:
        if smtp_starttls:
            smtp_client.starttls(context=ssl_context)
        if smtp_username and smtp_password:
            smtp_client.login(smtp_username, smtp_password)
        smtp_client.send_message(message)


def _run_monthly_report() -> Dict[str, Any]:
    subscription_id = _first_value(os.getenv("MONTHLY_REPORT_SUBSCRIPTION_ID"))
    if not subscription_id:
        raise CostManagementConfigError(
            "MONTHLY_REPORT_SUBSCRIPTION_ID must be configured for the monthly timer."
        )

    recipient = _first_value(
        os.getenv("MONTHLY_REPORT_RECIPIENT"),
        DEFAULT_MONTHLY_REPORT_RECIPIENT,
    )
    granularity = _first_value(
        os.getenv("MONTHLY_REPORT_GRANULARITY"),
        os.getenv("COST_QUERY_GRANULARITY"),
        "None",
    )
    start_date, end_date = _resolve_previous_month_range()
    delivery = _first_value(os.getenv("MONTHLY_REPORT_DELIVERY"), "blob")
    logging.info(
        "Preparing monthly report for subscription %s covering %s to %s with granularity %s via %s.",
        subscription_id,
        start_date,
        end_date,
        granularity,
        delivery,
    )
    result = _query_subscription_cost(
        subscription_id=subscription_id,
        timeframe="MonthToDate",
        granularity=granularity,
        start_date=start_date,
        end_date=end_date,
    )
    report_html = _render_html_report(result)
    report_filename = _build_monthly_report_filename(start_date)
    report_subject = f"Azure Cost Report - {start_date[:7]}"

    if delivery == "blob":
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
            "delivery": "blob",
            "container": container_name,
            "reportFilename": report_filename,
            "startDate": start_date,
            "endDate": end_date,
            "subscriptionId": subscription_id,
        }

    if delivery != "email":
        raise CostManagementConfigError(
            "MONTHLY_REPORT_DELIVERY must be either 'blob' or 'email'."
        )

    _send_email_attachment(
        recipient=recipient,
        subject=report_subject,
        attachment_name=report_filename,
        attachment_body=report_html,
    )
    logging.info("Sent monthly cost report to %s for %s.", recipient, start_date[:7])
    return {
        "delivery": "email",
        "recipient": recipient,
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
        "monthly_cost_report triggered. past_due=%s delivery=%s",
        monthly_timer.past_due,
        _first_value(os.getenv("MONTHLY_REPORT_DELIVERY"), "blob"),
    )
    if monthly_timer.past_due:
        logging.warning("The monthly cost report timer trigger is running late.")

    try:
        _run_monthly_report()
    except Exception:
        logging.exception("monthly_cost_report failed.")
        raise


@app.route(route="reports/monthly/run", methods=["POST"])
def run_monthly_report(req: func.HttpRequest) -> func.HttpResponse:
    try:
        result = _run_monthly_report()
        return _json_response(
            {
                "status": "ok",
                **result,
            }
        )
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


@app.route(route="cost/subscription", methods=["GET", "POST"])
def subscription_cost(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = _load_request_payload(req)
        subscription_id = _first_value(
            req.params.get("subscriptionId"),
            payload.get("subscriptionId"),
        )
        timeframe = _first_value(
            req.params.get("timeframe"),
            payload.get("timeframe"),
            os.getenv("COST_QUERY_TIMEFRAME"),
            "MonthToDate",
        )
        granularity = _first_value(
            req.params.get("granularity"),
            payload.get("granularity"),
            os.getenv("COST_QUERY_GRANULARITY"),
            "Daily",
        )
        start_date = _first_value(
            req.params.get("from"),
            req.params.get("startDate"),
            payload.get("from"),
            payload.get("startDate"),
        )
        end_date = _first_value(
            req.params.get("to"),
            req.params.get("endDate"),
            payload.get("to"),
            payload.get("endDate"),
        )
        output_format = _first_value(
            req.params.get("format"),
            payload.get("format"),
            "json",
        )

        if not subscription_id:
            raise CostManagementConfigError(
                "A subscription ID is required. Supply subscriptionId in the query string "
                "or request body."
            )

        result = _query_subscription_cost(
            subscription_id=subscription_id,
            timeframe=timeframe,
            granularity=granularity,
            start_date=start_date,
            end_date=end_date,
        )

        if output_format.lower() == "html":
            return _file_response(
                body=_render_html_report(result),
                mimetype="text/html",
                filename="cost-report.html",
                disposition="inline",
            )

        if output_format.lower() != "json":
            raise CostManagementConfigError(
                "Unsupported format. Supported values are: json, html."
            )

        return _file_response(
            body=json.dumps(result, indent=2),
            mimetype="application/json",
            filename="cost-report.json",
        )
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
        logging.exception("Unexpected failure while querying Azure Cost Management.")
        return _json_response(
            {
                "error": (
                    "Unexpected error while querying Azure Cost Management. "
                    "Check Function logs for details."
                )
            },
            status_code=500,
        )
