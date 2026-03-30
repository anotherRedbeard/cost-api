import unittest

from function_app import (
    CostManagementConfigError,
    _build_query_definition,
    _normalize_granularity,
    _normalize_query_result,
    _render_html_report,
    _resolve_time_period,
)


class FunctionAppHelpersTests(unittest.TestCase):
    def test_build_query_definition_uses_usage_and_aggregation(self) -> None:
        body = _build_query_definition(
            timeframe="MonthToDate",
            granularity="Daily",
        )

        self.assertEqual(body["type"], "Usage")
        self.assertEqual(body["timeframe"], "MonthToDate")
        self.assertEqual(
            body["dataset"]["aggregation"]["totalCost"],
            {"name": "PreTaxCost", "function": "Sum"},
        )
        self.assertEqual(body["dataset"]["granularity"], "Daily")

    def test_custom_date_range_overrides_timeframe(self) -> None:
        timeframe, time_period = _resolve_time_period(
            timeframe="MonthToDate",
            start_date="2026-03-01",
            end_date="2026-03-05",
        )

        self.assertEqual(timeframe, "Custom")
        self.assertEqual(
            time_period,
            {
                "from": "2026-03-01T00:00:00Z",
                "to": "2026-03-05T23:59:59Z",
            },
        )

    def test_custom_date_range_requires_both_dates(self) -> None:
        with self.assertRaises(CostManagementConfigError):
            _resolve_time_period(
                timeframe="MonthToDate",
                start_date="2026-03-01",
                end_date=None,
            )

    def test_normalize_query_result_formats_daily_breakdown(self) -> None:
        result = _normalize_query_result(
            subscription_id="sub-123",
            timeframe="MonthToDate",
            granularity="Daily",
            time_period=None,
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
                "timeframe": "MonthToDate",
                "granularity": "None",
                "currency": "USD",
                "totalCost": 42.5,
                "breakdown": [{"totalCost": 42.5, "currency": "USD"}],
            }
        )

        self.assertIn("Azure Cost Report", html_report)
        self.assertIn("sub-123", html_report)
        self.assertIn("42.5", html_report)


if __name__ == "__main__":
    unittest.main()
