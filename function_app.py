import datetime
import json
import requests
import os
import io
import logging
import traceback
import csv
import base64
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


def get_previous_month_range():
    """Calculate the first and last day of the previous month"""
    try:
        today = datetime.date.today()
        first_day_this_month = today.replace(day=1)
        last_day_prev_month = first_day_this_month - datetime.timedelta(days=1)
        first_day_prev_month = last_day_prev_month.replace(day=1)

        start_date_api = first_day_prev_month.isoformat()
        end_date_api = last_day_prev_month.isoformat()
        start_date_display = first_day_prev_month.strftime("%m-%d-%Y")
        end_date_display = last_day_prev_month.strftime("%m-%d-%Y")

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


def fetch_cost_for_subscription(token, subscription_id, start_date, end_date):
    """Fetch cost data for a specific subscription with detailed status reporting"""
    url = f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query?api-version=2023-03-01"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
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
                logger.warning(f"      Fix    : Check the Retry-After response header or reduce execution frequency")

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
    for item in all_costs_data:
        sub_name = item["subscription_name"]
        sub_id = item["subscription_id"]
        status_info = item.get("status_info", {})
        status_code = status_info.get("status_code", "N/A")
        success = status_info.get("success", False)
        reason = status_info.get("reason", "No information available")

        row_color = "#e8f5e9" if success else "#ffebee"
        status_icon = "" if success else ""

        rows_html += f"""
        <tr style="background-color: {row_color};">
            <td style="padding: 8px; border: 1px solid #ddd;">{status_icon} {sub_name}</td>
            <td style="padding: 8px; border: 1px solid #ddd; font-family: monospace; font-size: 0.85em;">{sub_id}</td>
            <td style="padding: 8px; border: 1px solid #ddd; text-align: center;"><strong>HTTP {status_code}</strong></td>
            <td style="padding: 8px; border: 1px solid #ddd; font-size: 0.9em;">{reason}</td>
        </tr>
        """

    summary_color = "#e8f5e9" if failed_count == 0 else "#fff3e0"

    return f"""
    <div style="margin: 20px 0;">
        <h3 style="color: #333;"> Subscription-wise API Status</h3>
        <div style="background-color: {summary_color}; padding: 10px; border-radius: 4px; margin-bottom: 10px;">
             Successful: <strong>{success_count}</strong> &nbsp;|&nbsp;
             Failed / No Data: <strong>{failed_count}</strong> &nbsp;|&nbsp;
            Total: <strong>{len(all_costs_data)}</strong>
        </div>
        <table style="width: 100%; border-collapse: collapse; font-size: 0.9em;">
            <thead>
                <tr style="background-color: #0078d4; color: white;">
                    <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Subscription</th>
                    <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">ID</th>
                    <th style="padding: 10px; border: 1px solid #ddd; text-align: center;">Status Code</th>
                    <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Details / Reason</th>
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
            <div style="background-color: #fff3e0; border-left: 4px solid #ff9800; padding: 15px; margin: 20px 0;">
                <strong> {len(failed_items)} subscription(s) encountered issues:</strong>
                <ul style="margin: 10px 0 0 0;">{failed_list}</ul>
            </div>
            """

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 800px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0078d4; border-bottom: 2px solid #0078d4; padding-bottom: 10px;">
                    Azure Cost Report - Scheduled
                </h2>

                <p>Hello,</p>

                <p>Please find your scheduled Azure cost report for the following period:</p>

                <div style="background-color: #f5f5f5; padding: 15px; border-left: 4px solid #0078d4; margin: 20px 0;">
                    <strong>Report Period:</strong> {start_date_display} to {end_date_display}<br>
                    <strong>Total Subscriptions:</strong> {subscription_count}<br>
                    <strong>Total Cost:</strong> <span style="font-size: 1.2em; color: #0078d4;">${total_cost:,.2f} USD</span>
                </div>

                {warning_html}
                {status_summary_html}

                <p>The detailed cost breakdown is attached as a CSV file: <strong>{filename}</strong></p>

                <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; color: #666; font-size: 0.9em;">
                    This is an automated report generated by Azure Function (Timer Trigger).<br>
                    Generated on: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} UTC
                </p>
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

@app.function_name(name="MonthlyReport")
@app.timer_trigger(schedule="0 0 0 1 * *", 
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
        start_date_api, end_date_api, start_date_display, end_date_display = get_previous_month_range()
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

        logger.info('=' * 80)
        logger.info(' Execution completed successfully!')
        logger.info(f'   Total Cost    : ${total_cost:,.2f} USD')
        logger.info(f'   Subscriptions : {len(all_costs_data)} ( {success_count} OK |  {failed_count} failed)')
        logger.info(f'   Report File   : {filename}')
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