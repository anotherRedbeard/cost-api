import json
import os
import unittest
from datetime import date
from unittest.mock import Mock, patch

import azure.functions as func
from azure.storage.blob import ContentSettings

from function_app import (
    CostManagementApiError,
    CostManagementConfigError,
    _build_monthly_report_filename,
    _build_report_run_id,
    _build_query_definition,
    _clear_cached_cost_queries,
    _get_blob_service_client,
    _normalize_granularity,
    _normalize_query_result,
    _query_cost_for_period,
    _render_html_report,
    _resolve_previous_month_range,
    _run_monthly_report,
    _send_email_attachment,
    _upload_report_to_blob,
    app,
    monthly_cost_report,
    run_monthly_report,
)


class FunctionAppHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        _clear_cached_cost_queries()

    def test_function_app_registers_expected_triggers(self) -> None:
        function_names = {function.get_function_name() for function in app.get_functions()}

        self.assertIn("monthly_cost_report", function_names)
        self.assertIn("run_monthly_report", function_names)
        self.assertIn("health", function_names)
        self.assertNotIn("_run_monthly_report", function_names)
        self.assertNotIn("subscription_cost", function_names)

    def test_build_query_definition_uses_custom_period(self) -> None:
        body = _build_query_definition(
            start_date="2026-03-01",
            end_date="2026-03-31",
            granularity="None",
        )

        self.assertEqual(body["type"], "Usage")
        self.assertEqual(body["timeframe"], "Custom")
        self.assertEqual(
            body["timePeriod"],
            {
                "from": "2026-03-01T00:00:00Z",
                "to": "2026-03-31T23:59:59Z",
            },
        )
        self.assertEqual(body["dataset"]["granularity"], "None")
        self.assertEqual(
            body["dataset"]["aggregation"]["totalCost"],
            {"name": "PreTaxCost", "function": "Sum"},
        )

    def test_normalize_query_result_formats_daily_breakdown(self) -> None:
        result = _normalize_query_result(
            subscription_id="sub-123",
            start_date="2026-03-01",
            end_date="2026-03-31",
            granularity="Daily",
            response_properties={
                "columns": [
                    {"name": "totalCost", "type": "Number"},
                    {"name": "UsageDate", "type": "Number"},
                    {"name": "Currency", "type": "String"},
                ],
                "rows": [
                    [10.25, 20260301, "USD"],
                    [5.75, 20260302, "USD"],
                ],
            },
        )

        self.assertEqual(result["totalCost"], 16.0)
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["periodStart"], "2026-03-01")
        self.assertEqual(result["periodEnd"], "2026-03-31")
        self.assertEqual(
            result["breakdown"],
            [
                {"totalCost": 10.25, "usageDate": "2026-03-01", "currency": "USD"},
                {"totalCost": 5.75, "usageDate": "2026-03-02", "currency": "USD"},
            ],
        )

    def test_normalize_granularity_rejects_invalid_values(self) -> None:
        with self.assertRaises(CostManagementConfigError):
            _normalize_granularity("Hourly")

    def test_render_html_report_contains_summary_values(self) -> None:
        html_report = _render_html_report(
            {
                "subscriptionId": "sub-123",
                "periodStart": "2026-03-01",
                "periodEnd": "2026-03-31",
                "granularity": "None",
                "currency": "USD",
                "totalCost": 42.5,
                "breakdown": [{"totalCost": 42.5, "currency": "USD"}],
            }
        )

        self.assertIn("Azure Cost Report", html_report)
        self.assertIn("sub-123", html_report)
        self.assertIn("2026-03-01", html_report)
        self.assertIn("42.5", html_report)

    def test_previous_month_range_uses_full_previous_calendar_month(self) -> None:
        self.assertEqual(
            _resolve_previous_month_range(date(2026, 3, 31)),
            ("2026-02-01", "2026-02-28"),
        )
        self.assertEqual(
            _resolve_previous_month_range(date(2026, 1, 10)),
            ("2025-12-01", "2025-12-31"),
        )

    def test_build_report_run_id_returns_short_hex_identifier(self) -> None:
        report_run_id = _build_report_run_id()

        self.assertEqual(len(report_run_id), 8)
        self.assertRegex(report_run_id, r"^[0-9a-f]{8}$")

    def test_build_monthly_report_filename_uses_year_month_and_run_id(self) -> None:
        self.assertEqual(
            _build_monthly_report_filename("2026-02-01", run_id="abc12345"),
            "cost-report-2026-02-abc12345.html",
        )

    def test_send_email_attachment_uses_smtp(self) -> None:
        smtp_client = Mock()
        smtp_context_manager = Mock()
        smtp_context_manager.__enter__ = Mock(return_value=smtp_client)
        smtp_context_manager.__exit__ = Mock(return_value=False)

        with patch.dict(
            os.environ,
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_PORT": "587",
                "SMTP_FROM": "reports@example.com",
                "SMTP_USERNAME": "reports@example.com",
                "SMTP_PASSWORD": "secret",
                "SMTP_STARTTLS": "true",
            },
            clear=False,
        ), patch(
            "function_app.smtplib.SMTP",
            return_value=smtp_context_manager,
        ):
            _send_email_attachment(
                recipient="andrew.redman@microsoft.com",
                subject="Azure Cost Report - 2026-02",
                attachment_name="cost-report-2026-02.html",
                attachment_body="<html>report</html>",
            )

        smtp_client.starttls.assert_called_once()
        smtp_client.login.assert_called_once_with("reports@example.com", "secret")
        smtp_client.send_message.assert_called_once()

    def test_get_blob_service_client_prefers_connection_string(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AzureWebJobsStorage": "UseDevelopmentStorage=true",
            },
            clear=False,
        ), patch(
            "function_app.BlobServiceClient.from_connection_string"
        ) as from_connection_string:
            _get_blob_service_client()

        from_connection_string.assert_called_once_with("UseDevelopmentStorage=true")

    def test_upload_report_to_blob_writes_html(self) -> None:
        container_client = Mock()
        blob_service_client = Mock()
        blob_service_client.get_container_client.return_value = container_client

        with patch(
            "function_app._get_blob_service_client",
            return_value=blob_service_client,
        ):
            _upload_report_to_blob(
                container_name="monthly-cost-reports",
                blob_name="cost-report-2026-02.html",
                report_html="<html>report</html>",
            )

        container_client.create_container.assert_called_once()
        container_client.upload_blob.assert_called_once_with(
            name="cost-report-2026-02.html",
            data=b"<html>report</html>",
            overwrite=True,
            content_settings=ContentSettings(content_type="text/html; charset=utf-8"),
        )

    def test_run_monthly_report_queries_previous_month(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MONTHLY_REPORT_DELIVERY": "blob",
                "MONTHLY_REPORT_SUBSCRIPTION_ID": "sub-123",
            },
            clear=False,
        ), patch(
            "function_app._resolve_previous_month_range",
            return_value=("2026-02-01", "2026-02-28"),
        ), patch(
            "function_app._query_cost_for_period",
            return_value={
                "subscriptionId": "sub-123",
                "periodStart": "2026-02-01",
                "periodEnd": "2026-02-28",
                "granularity": "None",
                "currency": "USD",
                "totalCost": 42.5,
                "breakdown": [{"totalCost": 42.5, "currency": "USD"}],
            },
        ) as query_cost_for_period, patch(
            "function_app._upload_report_to_blob"
        ) as upload_report_to_blob:
            with patch("function_app._build_report_run_id", return_value="abc12345"):
                result = _run_monthly_report()

        query_cost_for_period.assert_called_once_with(
            subscription_id="sub-123",
            start_date="2026-02-01",
            end_date="2026-02-28",
            granularity="None",
        )
        upload_report_to_blob.assert_called_once_with(
            container_name="monthly-cost-reports",
            blob_name="cost-report-2026-02-abc12345.html",
            report_html=unittest.mock.ANY,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["delivery"], "blob")

    def test_monthly_cost_report_uploads_blob_when_configured(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MONTHLY_REPORT_DELIVERY": "blob",
                "MONTHLY_REPORT_SUBSCRIPTION_ID": "sub-123",
            },
            clear=False,
        ), patch(
            "function_app._run_monthly_report"
        ) as run_monthly_report_impl:
            monthly_cost_report(Mock(past_due=False))

        run_monthly_report_impl.assert_called_once_with()

    def test_run_monthly_report_returns_delivery_details(self) -> None:
        request = func.HttpRequest(
            method="POST",
            url="http://localhost/api/reports/monthly/run",
            params={},
            body=b"",
        )

        with patch(
            "function_app._run_monthly_report",
            return_value={
                "status": "ok",
                "delivery": "blob",
                "container": "monthly-cost-reports",
                "reportFilename": "cost-report-2026-02-abc12345.html",
                "startDate": "2026-02-01",
                "endDate": "2026-02-28",
                "subscriptionId": "sub-123",
            },
        ):
            response = run_monthly_report(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.get_body().decode("utf-8")),
            {
                "status": "ok",
                "delivery": "blob",
                "container": "monthly-cost-reports",
                "reportFilename": "cost-report-2026-02-abc12345.html",
                "startDate": "2026-02-01",
                "endDate": "2026-02-28",
                "subscriptionId": "sub-123",
            },
        )

    def test_run_monthly_report_accepts_get_requests(self) -> None:
        request = func.HttpRequest(
            method="GET",
            url="http://localhost/api/reports/monthly/run",
            params={},
            body=b"",
        )

        with patch(
            "function_app._run_monthly_report",
            return_value={
                "status": "ok",
                "delivery": "blob",
                "container": "monthly-cost-reports",
                "reportFilename": "cost-report-2026-02-abc12345.html",
                "startDate": "2026-02-01",
                "endDate": "2026-02-28",
                "subscriptionId": "sub-123",
            },
        ):
            response = run_monthly_report(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.get_body().decode("utf-8"))["status"],
            "ok",
        )

    def test_run_monthly_report_returns_config_errors(self) -> None:
        request = func.HttpRequest(
            method="GET",
            url="http://localhost/api/reports/monthly/run",
            params={},
            body=b"",
        )

        with patch(
            "function_app._run_monthly_report",
            side_effect=CostManagementConfigError("missing config"),
        ):
            response = run_monthly_report(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.get_body().decode("utf-8")),
            {"error": "missing config"},
        )

    def test_run_monthly_report_surfaces_cost_api_errors(self) -> None:
        request = func.HttpRequest(
            method="POST",
            url="http://localhost/api/reports/monthly/run",
            params={},
            body=b"",
        )

        with patch(
            "function_app._run_monthly_report",
            side_effect=CostManagementApiError(
                status_code=429,
                message="Throttled.",
                retry_after="15",
            ),
        ):
            response = run_monthly_report(request)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("Retry-After"), "15")

    def test_query_cost_for_period_reuses_cached_result(self) -> None:
        response = Mock(
            status_code=200,
            headers={},
        )
        response.json.return_value = {
            "properties": {
                "columns": [
                    {"name": "totalCost", "type": "Number"},
                    {"name": "UsageDate", "type": "Number"},
                    {"name": "Currency", "type": "String"},
                ],
                "rows": [
                    [12.34, 20260301, "USD"],
                ],
            }
        }

        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.post.return_value = response

        with patch("function_app._get_access_token", return_value="token"), patch(
            "function_app.requests.Session", return_value=session
        ):
            first_result = _query_cost_for_period(
                subscription_id="sub-123",
                start_date="2026-03-01",
                end_date="2026-03-31",
                granularity="None",
            )
            second_result = _query_cost_for_period(
                subscription_id="sub-123",
                start_date="2026-03-01",
                end_date="2026-03-31",
                granularity="None",
            )

        self.assertEqual(first_result, second_result)
        session.post.assert_called_once()
        self.assertEqual(first_result["totalCost"], 12.34)
        self.assertEqual(first_result["periodStart"], "2026-03-01")
        self.assertEqual(first_result["periodEnd"], "2026-03-31")


if __name__ == "__main__":
    unittest.main()
