import datetime
import json
import requests
import os
import io
import logging
import traceback
import csv
import base64
import time
from azure.communication.email import EmailClient
import azure.functions as func
app = func.FunctionApp()

# Setup logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Azure Cost Management error codes and their human-readable reasons
COST_MANAGEMENT_ERROR_REASONS = {
    400: "Bad Request - Invalid query body, date format, or missing required fields",
    401: "Unauthorized - Access token is expired or invalid",
    403: "Forbidden - Service principal does not have Cost Management access for this subscription",
    404: "Not Found - Subscription does not exist or Cost Management provider is not registered",
    429: "Too Many Requests - API rate limit exceeded, retry after some time",
    500: "Internal Server Error - Azure side issue, retry later",
    501: "Not Implemented - This feature is not supported for this subscription type",
    503: "Service Unavailable - Azure Cost Management service is temporarily down",
}

def get_status_reason(status_code, response_text=""):
    """Get human-readable reason for a status code"""
    base_reason = COST_MANAGEMENT_ERROR_REASONS.get(status_code, f"Unknown error (HTTP {status_code})")

    try:
        error_body = json.loads(response_text)
        error_details = error_body.get("error", {})
        error_code = error_details.get("code", "")
        error_message = error_details.get("message", "")

        if error_code or error_message:
            return f"{base_reason} | Azure Error Code: {error_code} | Message: {error_message}"
    except (json.JSONDecodeError, AttributeError):
        if response_text:
            snippet = response_text[:200] + "..." if len(response_text) > 200 else response_text
            return f"{base_reason} | Raw Response: {snippet}"

    return base_reason


def get_access_token():
    """Get Azure access token using service principal credentials"""
    try:
        logger.info("Starting token acquisition...")

        TENANT_ID = os.environ.get("TENANT_ID")
        CLIENT_ID = os.environ.get("CLIENT_ID")
        CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

        if not TENANT_ID:
            raise ValueError("TENANT_ID environment variable is not set")
        if not CLIENT_ID:
            raise ValueError("CLIENT_ID environment variable is not set")
        if not CLIENT_SECRET:
            raise ValueError("CLIENT_SECRET environment variable is not set")

        logger.info(f"TENANT_ID: {TENANT_ID[:8]}... (length: {len(TENANT_ID)})")
        logger.info(f"CLIENT_ID: {CLIENT_ID[:8]}... (length: {len(CLIENT_ID)})")
        logger.info(f"CLIENT_SECRET: {'*' * 8}... (length: {len(CLIENT_SECRET)})")

        url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "resource": "https://management.azure.com/"
        }

        logger.info(f"Requesting token from: {url}")
        response = requests.post(url, data=payload, timeout=30)

        logger.info(f"Token API Response Status: HTTP {response.status_code}")

        if response.status_code != 200:
            reason = get_status_reason(response.status_code, response.text)
            logger.error(f"   Token request FAILED")
            logger.error(f"   Status Code : {response.status_code}")
            logger.error(f"   Reason      : {reason}")
            response.raise_for_status()

        token_data = response.json()
        logger.info(" Access token acquired successfully (HTTP 200 OK)")
        return token_data["access_token"]

    except requests.exceptions.Timeout:
        logger.error(" Token request TIMED OUT after 30 seconds")
        raise Exception("Authentication timeout: Login endpoint did not respond within 30 seconds")
    except requests.exceptions.RequestException as e:
        logger.error(f" Token request FAILED with network error: {str(e)}")
        raise Exception(f"Authentication failed: {str(e)}")
    except KeyError as e:
        logger.error(f" 'access_token' field missing in token response: {str(e)}")
        raise Exception(f"Invalid token response: {str(e)}")
    except Exception as e:
        logger.error(f" Unexpected error during token acquisition: {type(e).__name__}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise


def get_current_month_range():
    """Calculate the first day and today for the current month"""
    try:
        today = datetime.date.today()
        first_day = today.replace(day=1)

        start_date_api = first_day.isoformat()
        end_date_api = today.isoformat()
        start_date_display = first_day.strftime("%m-%d-%Y")
        end_date_display = today.strftime("%m-%d-%Y")

        logger.info(f"Date range calculated: {start_date_display} to {end_date_display}")
        return start_date_api, end_date_api, start_date_display, end_date_display

    except Exception as e:
        logger.error(f"Error calculating date range: {str(e)}")
        raise


def get_all_subscriptions(token):
    """Fetch all subscriptions accessible to the service principal"""
    try:
        logger.info("Fetching subscriptions...")
        url = "https://management.azure.com/subscriptions?api-version=2020-01-01"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        response = requests.get(url, headers=headers, timeout=30)

        logger.info(f"Subscriptions API Response Status: HTTP {response.status_code}")

        if response.status_code != 200:
            reason = get_status_reason(response.status_code, response.text)
            logger.error(f"   Subscription fetch FAILED")
            logger.error(f"   Status Code : {response.status_code}")
            logger.error(f"   Reason      : {reason}")
            response.raise_for_status()

        subscriptions = response.json().get("value", [])
        logger.info(f"Subscriptions fetched successfully (HTTP 200 OK) - Found: {len(subscriptions)}")

        if not subscriptions:
            logger.warning("  No subscriptions found for this service principal")
            logger.warning("   Possible reasons:")
            logger.warning("   1. Service principal has not been granted access to any subscription")
            logger.warning("   2. Subscriptions are disabled or cancelled")
            logger.warning("   3. Service principal belongs to a different tenant")
        else:
            for sub in subscriptions[:3]:
                logger.info(f"   - {sub.get('displayName')} ({sub.get('subscriptionId')})")

        return subscriptions

    except requests.exceptions.Timeout:
        logger.error("Subscription fetch TIMED OUT after 30 seconds")
        raise Exception("Subscription fetch timeout")
    except requests.exceptions.RequestException as e:
        logger.error(f"Subscription fetch FAILED: {str(e)}")
        raise Exception(f"Failed to fetch subscriptions: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error: {type(e).__name__}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise


def fetch_cost_for_subscription(token, subscription_id, start_date, end_date, max_retries=3):
    """Fetch cost data for a specific subscription with detailed status reporting.

    Passes a ClientType header so calls use a dedicated per-client quota rather
    than the shared anonymous bucket, which is the primary cause of 429s when
    many tenants omit this header.  Retries up to max_retries times on 429,
    honouring the Retry-After header returned by the API.
    """
    url = f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query?api-version=2023-03-01"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "ClientType": "CostReportFunction",
    }
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start_date,
            "to": end_date
        },
        "dataset": {
            "granularity": "None",
            "aggregation": {
                "totalCost": {
                    "name": "Cost",
                    "function": "Sum"
                }
            }
        }
    }

    # Tracks status for each subscription result
    status_info = {
        "status_code": None,
        "success": False,
        "reason": "",
        "has_data": False,
        "row_count": 0
    }

    attempt = 0
    while attempt <= max_retries:
        try:
            response = requests.post(url, headers=headers, json=body, timeout=60)
            status_info["status_code"] = response.status_code

            if response.status_code == 200:
                cost_data = response.json()
                rows = cost_data.get("properties", {}).get("rows", [])

                status_info["success"] = True
                status_info["row_count"] = len(rows)
                status_info["has_data"] = len(rows) > 0

                if rows:
                    cost_value = rows[0][0] if len(rows[0]) > 0 else 0
                    currency = rows[0][1] if len(rows[0]) > 1 else "USD"
                    status_info["reason"] = f"Cost data retrieved: {cost_value:.2f} {currency}"
                    logger.info(f"    HTTP 200 OK | Cost: {cost_value:.2f} {currency} | Rows: {len(rows)}")
                else:
                    # 200 returned but no rows — typical for $0 usage subscriptions
                    status_info["reason"] = "HTTP 200 OK but no cost data for this period (possibly $0 usage or no resources)"
                    logger.info(f"    HTTP 200 OK |   No rows returned (zero usage or no active resources)")

                return cost_data, status_info

            elif response.status_code == 429 and attempt < max_retries:
                # Honour the Retry-After header; fall back to 60 s if absent
                retry_after = int(
                    response.headers.get(
                        "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after",
                        response.headers.get("Retry-After", 60)
                    )
                )
                attempt += 1
                logger.warning(
                    f"    HTTP 429 — rate limited (attempt {attempt}/{max_retries}). "
                    f"Waiting {retry_after}s before retry..."
                )
                time.sleep(retry_after)
                continue

            else:
                reason = get_status_reason(response.status_code, response.text)
                status_info["reason"] = reason

                logger.warning(f"    HTTP {response.status_code} FAILED")
                logger.warning(f"      Reason : {reason}")

                if response.status_code == 403:
                    logger.warning(f"      Fix    : Assign 'Cost Management Reader' role to the service principal on subscription '{subscription_id}'")
                elif response.status_code == 404:
                    logger.warning(f"      Fix    : Verify the subscription is active and Microsoft.CostManagement provider is registered")
                elif response.status_code == 429:
                    logger.warning(f"      Fix    : Max retries exceeded — still rate limited after {max_retries} attempts")

                return {"properties": {"rows": [], "columns": []}}, status_info

        except requests.exceptions.Timeout:
            status_info["status_code"] = "TIMEOUT"
            status_info["reason"] = "Request did not complete within 60 seconds (subscription may be busy or network issue)"
            logger.warning(f"    TIMEOUT | {status_info['reason']}")
            return {"properties": {"rows": [], "columns": []}}, status_info

        except requests.exceptions.ConnectionError as e:
            status_info["status_code"] = "CONNECTION_ERROR"
            status_info["reason"] = f"Network connection failed: {str(e)}"
            logger.warning(f"    CONNECTION ERROR | {status_info['reason']}")
            return {"properties": {"rows": [], "columns": []}}, status_info

        except Exception as e:
            status_info["status_code"] = "EXCEPTION"
            status_info["reason"] = f"{type(e).__name__}: {str(e)}"
            logger.warning(f"    EXCEPTION | {status_info['reason']}")
            return {"properties": {"rows": [], "columns": []}}, status_info

    # Should not be reached, but return empty data if loop exhausted
    return {"properties": {"rows": [], "columns": []}}, status_info


def generate_csv(all_costs_data, start_date_display, end_date_display):
    """Generate CSV with all subscriptions cost data including status columns"""
    try:
        logger.info("Generating CSV file...")

        csv_buffer = io.StringIO()
        csv_writer = csv.writer(csv_buffer)

        csv_writer.writerow([
            "Subscription Name",
            "Subscription ID",
            f"Total Cost ({start_date_display} to {end_date_display})",
            "Currency",
            "API Status Code",
            "Status / Reason"
        ])

        total_cost = 0

        for cost_item in all_costs_data:
            sub_name = cost_item["subscription_name"]
            sub_id = cost_item["subscription_id"]
            cost_data = cost_item["cost_data"]
            status_info = cost_item.get("status_info", {})

            rows = cost_data.get("properties", {}).get("rows", [])
            api_status = status_info.get("status_code", "N/A")
            status_reason = status_info.get("reason", "")

            if rows:
                cost = rows[0][0] if len(rows[0]) > 0 else 0
                currency = rows[0][1] if len(rows[0]) > 1 else "USD"
                total_cost += cost
                csv_writer.writerow([sub_name, sub_id, f"{cost:.2f}", currency, api_status, status_reason])
            else:
                no_data_reason = status_reason if status_reason else "No cost data returned"
                csv_writer.writerow([sub_name, sub_id, "0.00", "USD", api_status, no_data_reason])

        csv_writer.writerow([])
        csv_writer.writerow(["TOTAL", "", f"{total_cost:.2f}", "USD", "", ""])

        csv_content = csv_buffer.getvalue()
        logger.info(f"CSV generated | Subscriptions: {len(all_costs_data)} | Total Cost: ${total_cost:.2f}")
        return csv_content, total_cost

    except Exception as e:
        logger.error(f"Error generating CSV: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise


def build_status_summary_html(all_costs_data):
    """Build a subscription-wise API status summary table for the email body"""
    success_count = sum(1 for item in all_costs_data if item.get("status_info", {}).get("success"))
    failed_count = len(all_costs_data) - success_count

    rows_html = ""
    for i, item in enumerate(all_costs_data):
        sub_name = item["subscription_name"]
        sub_id = item["subscription_id"]
        status_info = item.get("status_info", {})
        status_code = status_info.get("status_code", "N/A")
        success = status_info.get("success", False)
        reason = status_info.get("reason", "No information available")

        row_bg = "#ffffff" if i % 2 == 0 else "#f8fafc"
        badge_bg = "#dcfce7" if success else "#fee2e2"
        badge_color = "#166534" if success else "#991b1b"
        badge_label = f"✓ HTTP {status_code}" if success else f"✗ HTTP {status_code}"

        rows_html += f"""
        <tr style="background-color: {row_bg}; border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 12px 16px; font-weight: 500; color: #1e293b;">{sub_name}</td>
            <td style="padding: 12px 16px; font-family: monospace; font-size: 0.8em; color: #64748b;">{sub_id}</td>
            <td style="padding: 12px 16px; text-align: center;">
                <span style="display: inline-block; background-color: {badge_bg}; color: {badge_color}; font-size: 0.78em; font-weight: 600; padding: 3px 10px; border-radius: 12px; white-space: nowrap;">{badge_label}</span>
            </td>
            <td style="padding: 12px 16px; font-size: 0.875em; color: #475569;">{reason}</td>
        </tr>
        """

    summary_bg = "#f0fdf4" if failed_count == 0 else "#fffbeb"
    summary_border = "#86efac" if failed_count == 0 else "#fcd34d"

    return f"""
    <div style="margin: 28px 0;">
        <h3 style="font-size: 1em; font-weight: 600; color: #1e293b; text-transform: uppercase; letter-spacing: 0.05em; margin: 0 0 12px 0;">Subscription API Status</h3>
        <div style="background-color: {summary_bg}; border: 1px solid {summary_border}; border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; font-size: 0.9em; color: #374151;">
            <span style="margin-right: 20px;">✓ <strong>{success_count}</strong> Successful</span>
            <span style="margin-right: 20px;">✗ <strong>{failed_count}</strong> Failed / No Data</span>
            <span>&#9632; <strong>{len(all_costs_data)}</strong> Total</span>
        </div>
        <table style="width: 100%; border-collapse: collapse; font-size: 0.9em; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;">
            <thead>
                <tr style="background-color: #1e293b; color: #f1f5f9;">
                    <th style="padding: 12px 16px; text-align: left; font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;">Subscription</th>
                    <th style="padding: 12px 16px; text-align: left; font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;">Subscription ID</th>
                    <th style="padding: 12px 16px; text-align: center; font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;">Status</th>
                    <th style="padding: 12px 16px; text-align: left; font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;">Details</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
    """


def send_email_with_csv_attachment(csv_content, filename, start_date_display, end_date_display, total_cost, subscription_count, all_costs_data):
    """Send email with CSV attachment using Azure Communication Services"""
    try:
        logger.info("Preparing to send email via Azure Communication Services...")

        ACS_CONNECTION_STRING = os.environ.get("ACS_CONNECTION_STRING")
        ACS_SENDER_EMAIL = os.environ.get("ACS_SENDER_EMAIL")
        ACS_RECIPIENT_EMAIL = os.environ.get("ACS_RECIPIENT_EMAIL")

        if not ACS_CONNECTION_STRING:
            raise ValueError("ACS_CONNECTION_STRING environment variable is not set")
        if not ACS_SENDER_EMAIL:
            raise ValueError("ACS_SENDER_EMAIL environment variable is not set")
        if not ACS_RECIPIENT_EMAIL:
            raise ValueError("ACS_RECIPIENT_EMAIL environment variable is not set")

        logger.info(f"Sender email: {ACS_SENDER_EMAIL}")
        logger.info(f"Recipient email(s): {ACS_RECIPIENT_EMAIL}")

        email_client = EmailClient.from_connection_string(ACS_CONNECTION_STRING)
        recipient_emails = [e.strip() for e in ACS_RECIPIENT_EMAIL.replace(';', ',').split(',') if e.strip()]

        if not recipient_emails:
            raise ValueError("No valid recipient emails found in ACS_RECIPIENT_EMAIL")

        logger.info(f"Sending to {len(recipient_emails)} recipient(s): {', '.join(recipient_emails)}")

        csv_base64 = base64.b64encode(csv_content.encode('utf-8')).decode('utf-8')

        status_summary_html = build_status_summary_html(all_costs_data)

        failed_items = [item for item in all_costs_data if not item.get("status_info", {}).get("success")]
        warning_html = ""
        if failed_items:
            failed_list = "".join(
                f"<li><strong>{item['subscription_name']}</strong> — "
                f"HTTP {item['status_info'].get('status_code', 'N/A')}: "
                f"{item['status_info'].get('reason', 'Unknown error')}</li>"
                for item in failed_items
            )
            warning_html = f"""
            <div style="background-color: #fffbeb; border: 1px solid #fcd34d; border-left: 4px solid #f59e0b; border-radius: 8px; padding: 16px 20px; margin: 24px 0;">
                <p style="margin: 0 0 8px 0; font-weight: 700; color: #92400e; font-size: 0.95em;">⚠ {len(failed_items)} subscription(s) encountered issues</p>
                <ul style="margin: 0; padding-left: 20px; color: #78350f; font-size: 0.875em; line-height: 1.7;">{failed_list}</ul>
            </div>
            """

        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
        <body style="margin: 0; padding: 0; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; color: #1e293b;">
            <div style="max-width: 760px; margin: 32px auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.07);">

                <!-- Header -->
                <div style="background: linear-gradient(135deg, #0f172a 0%, #1e40af 100%); padding: 36px 40px;">
                    <p style="margin: 0 0 6px 0; font-size: 0.75em; font-weight: 600; color: #93c5fd; text-transform: uppercase; letter-spacing: 0.1em;">Azure Cost Management</p>
                    <h1 style="margin: 0; font-size: 1.6em; font-weight: 700; color: #ffffff; line-height: 1.2;">Monthly Cost Report</h1>
                    <p style="margin: 8px 0 0 0; font-size: 0.875em; color: #93c5fd;">{start_date_display} &mdash; {end_date_display}</p>
                </div>

                <!-- Body -->
                <div style="padding: 36px 40px;">
                    <p style="margin: 0 0 28px 0; font-size: 0.95em; color: #475569;">Here is your scheduled Azure cost summary. The full breakdown is attached as a CSV file.</p>

                    <!-- Metric Cards -->
                    <table style="width: 100%; border-collapse: separate; border-spacing: 12px; margin: 0 -12px 12px -12px;">
                        <tr>
                            <td style="width: 33%; background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 20px 24px; vertical-align: top;">
                                <p style="margin: 0 0 6px 0; font-size: 0.72em; font-weight: 600; color: #64748b; text-transform: uppercase; letter-spacing: 0.07em;">Report Period</p>
                                <p style="margin: 0; font-size: 0.95em; font-weight: 600; color: #1e293b;">{start_date_display}</p>
                                <p style="margin: 2px 0 0 0; font-size: 0.8em; color: #64748b;">to {end_date_display}</p>
                            </td>
                            <td style="width: 33%; background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 20px 24px; vertical-align: top;">
                                <p style="margin: 0 0 6px 0; font-size: 0.72em; font-weight: 600; color: #64748b; text-transform: uppercase; letter-spacing: 0.07em;">Subscriptions</p>
                                <p style="margin: 0; font-size: 1.6em; font-weight: 700; color: #1e293b;">{subscription_count}</p>
                            </td>
                            <td style="width: 33%; background: linear-gradient(135deg, #1e40af, #2563eb); border-radius: 10px; padding: 20px 24px; vertical-align: top;">
                                <p style="margin: 0 0 6px 0; font-size: 0.72em; font-weight: 600; color: #bfdbfe; text-transform: uppercase; letter-spacing: 0.07em;">Total Cost</p>
                                <p style="margin: 0; font-size: 1.6em; font-weight: 700; color: #ffffff;">${total_cost:,.2f}</p>
                                <p style="margin: 2px 0 0 0; font-size: 0.8em; color: #bfdbfe;">USD</p>
                            </td>
                        </tr>
                    </table>

                    {warning_html}
                    {status_summary_html}

                    <p style="margin: 24px 0 0 0; font-size: 0.875em; color: #475569;">
                        The detailed cost breakdown is attached: <strong style="color: #1e293b;">{filename}</strong>
                    </p>
                </div>

                <!-- Footer -->
                <div style="background-color: #f8fafc; border-top: 1px solid #e2e8f0; padding: 20px 40px;">
                    <p style="margin: 0; font-size: 0.8em; color: #94a3b8; line-height: 1.6;">
                        Automated report &bull; Azure Function (Timer Trigger) &bull; Generated {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} UTC
                    </p>
                </div>

            </div>
        </body>
        </html>
        """

        message = {
            "senderAddress": ACS_SENDER_EMAIL,
            "recipients": {"to": [{"address": email} for email in recipient_emails]},
            "content": {
                "subject": f"Azure Cost Report - {start_date_display} to {end_date_display}",
                "html": html_content
            },
            "attachments": [{
                "name": filename,
                "contentType": "text/csv",
                "contentInBase64": csv_base64
            }]
        }

        logger.info("Sending email via ACS with CSV attachment...")
        poller = email_client.begin_send(message)
        result = poller.result()

        logger.info(f"   Email sent successfully!")
        logger.info(f"   Message ID : {result['id']}")
        logger.info(f"   Status     : {result['status']}")
        logger.info(f"   Recipients : {', '.join(recipient_emails)}")
        logger.info(f"   Attachment : {filename}")
        return True

    except ValueError as ve:
        logger.error(f" Email configuration error: {str(ve)}")
        raise
    except Exception as e:
        logger.error(f" Email send FAILED: {type(e).__name__}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise

def _run_email_cost_report():
    """Shared logic for the email cost report, used by both timer and HTTP triggers."""
    # Step 1: Validate environment variables
    logger.info("Step 1: Validating environment variables...")
    required_vars = [
        "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET",
        "ACS_CONNECTION_STRING", "ACS_SENDER_EMAIL", "ACS_RECIPIENT_EMAIL"
    ]
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")
    logger.info(" All environment variables present")

    # Step 2: Get access token
    logger.info("Step 2: Acquiring Azure access token...")
    token = get_access_token()

    # Step 3: Calculate date range
    logger.info("Step 3: Calculating date range...")
    start_date_api, end_date_api, start_date_display, end_date_display = get_current_month_range()
    logger.info(f"Date range: {start_date_display} to {end_date_display}")

    # Step 4: Fetch subscriptions
    logger.info("Step 4: Fetching all subscriptions...")
    subscriptions = get_all_subscriptions(token)
    if not subscriptions:
        raise Exception("No subscriptions found - the service principal does not have access to any subscriptions")
    logger.info(f"Found {len(subscriptions)} subscription(s)")

    # Step 5: Fetch cost data for each subscription
    logger.info("Step 5: Fetching cost data for all subscriptions...")
    all_costs_data = []

    for idx, subscription in enumerate(subscriptions, 1):
        sub_id = subscription.get("subscriptionId")
        sub_name = subscription.get("displayName", "Unknown")
        logger.info(f"  [{idx}/{len(subscriptions)}] Processing: {sub_name} ({sub_id})")

        cost_data, status_info = fetch_cost_for_subscription(token, sub_id, start_date_api, end_date_api)

        all_costs_data.append({
            "subscription_id": sub_id,
            "subscription_name": sub_name,
            "cost_data": cost_data,
            "status_info": status_info
        })

    # Step 5 summary
    success_count = sum(1 for item in all_costs_data if item["status_info"]["success"])
    failed_count = len(all_costs_data) - success_count
    logger.info(f"Step 5 Summary:  {success_count} succeeded |  {failed_count} failed | Total: {len(all_costs_data)}")

    if failed_count > 0:
        logger.warning("Failed subscriptions detail:")
        for item in all_costs_data:
            if not item["status_info"]["success"]:
                logger.warning(
                    f"  - {item['subscription_name']}: "
                    f"HTTP {item['status_info']['status_code']} — {item['status_info']['reason']}"
                )

    # Step 6: Generate CSV
    logger.info("Step 6: Generating CSV report...")
    csv_content, total_cost = generate_csv(all_costs_data, start_date_display, end_date_display)
    filename = f"azure_cost_report_{start_date_display}_to_{end_date_display}.csv"
    logger.info("CSV report generated")

    # Step 7: Send email
    logger.info("Step 7: Sending email with CSV attachment...")
    send_email_with_csv_attachment(
        csv_content, filename, start_date_display, end_date_display,
        total_cost, len(all_costs_data), all_costs_data
    )
    logger.info("Email sent successfully")

    return {
        "status": "ok",
        "totalCost": f"${total_cost:,.2f} USD",
        "subscriptions": len(all_costs_data),
        "succeeded": success_count,
        "failed": failed_count,
        "reportFile": filename
    }


@app.function_name(name="MonthlyReport")
@app.timer_trigger(schedule="%MONTHLY_REPORT_SCHEDULE%",
              arg_name="mytimer",
              run_on_startup=True) 
def main(mytimer: func.TimerRequest) -> None:
    """Main function entry point for timer trigger"""
    logger.info('=' * 80)
    logger.info('Azure Cost Report - Timer Triggered Execution Starting')
    logger.info('=' * 80)

    if mytimer.past_due:
        logger.info(' The timer is past due!')

    logger.info(f'Timer trigger fired at: {datetime.datetime.utcnow()} UTC')

    try:
        result = _run_email_cost_report()

        logger.info('=' * 80)
        logger.info(' Execution completed successfully!')
        logger.info(f'   Total Cost    : {result["totalCost"]}')
        logger.info(f'   Subscriptions : {result["subscriptions"]} ( {result["succeeded"]} OK |  {result["failed"]} failed)')
        logger.info(f'   Report File   : {result["reportFile"]}')
        logger.info('=' * 80)

    except ValueError as ve:
        logger.error('=' * 80)
        logger.error(' EXECUTION FAILED - Configuration Error')
        logger.error(f'   {str(ve)}')
        logger.error('   Please configure the missing variables in Azure Function App Settings')
        logger.error('=' * 80)
        raise

    except requests.exceptions.RequestException as re:
        logger.error('=' * 80)
        logger.error(' EXECUTION FAILED - Azure API Error')
        logger.error(f'   {str(re)}')
        logger.error('=' * 80)
        raise

    except Exception as e:
        logger.error('=' * 80)
        logger.error(' EXECUTION FAILED - Unexpected Error')
        logger.error(f'   {type(e).__name__}: {str(e)}')
        logger.error(f'   Traceback: {traceback.format_exc()}')
        logger.error('=' * 80)
        raise


@app.function_name(name="RunEmailCostReport")
@app.route(route="reports/email/run", methods=["GET", "POST"])
def run_email_cost_report(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP trigger to manually run the email cost report."""
    logger.info('=' * 80)
    logger.info('Azure Cost Report - HTTP Triggered Execution Starting')
    logger.info('=' * 80)

    try:
        result = _run_email_cost_report()
        return func.HttpResponse(
            body=json.dumps(result, indent=2),
            status_code=200,
            mimetype="application/json"
        )
    except ValueError as ve:
        return func.HttpResponse(
            body=json.dumps({"error": str(ve)}, indent=2),
            status_code=400,
            mimetype="application/json"
        )
    except Exception as e:
        logger.error(f"HTTP trigger failed: {type(e).__name__}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return func.HttpResponse(
            body=json.dumps({"error": f"{type(e).__name__}: {str(e)}"}, indent=2),
            status_code=500,
            mimetype="application/json"
        )