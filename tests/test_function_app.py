"""
Tests for function_app.py — covers date helpers, CSV generation,
cost row extraction, environment validation, and HTTP endpoint behaviour.
"""
import datetime
import os
import unittest
from unittest.mock import patch

import azure.functions as func

from function_app import (
    _extract_cost_row,
    _validate_environment,
    generate_csv,
    generate_history_csv,
    get_current_month_range,
    get_last_n_months,
    run_email_cost_report,
    run_history_cost_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cost_item(name, sub_id, cost, currency="USD", success=True, status_code=200, reason=""):
    rows = [[cost, currency]] if cost is not None else []
    return {
        "subscription_name": name,
        "subscription_id": sub_id,
        "cost_data": {"properties": {"rows": rows}},
        "status_info": {
            "status_code": status_code,
            "success": success,
            "reason": reason,
        },
    }


# ---------------------------------------------------------------------------
# get_current_month_range
# ---------------------------------------------------------------------------

class TestGetCurrentMonthRange(unittest.TestCase):
    def test_start_is_first_day_of_month(self):
        start_api, _, _, _ = get_current_month_range()
        today = datetime.date.today()
        self.assertEqual(start_api, today.replace(day=1).isoformat())

    def test_end_is_today(self):
        _, end_api, _, _ = get_current_month_range()
        self.assertEqual(end_api, datetime.date.today().isoformat())


# ---------------------------------------------------------------------------
# get_last_n_months
# ---------------------------------------------------------------------------

class TestGetLastNMonths(unittest.TestCase):
    def test_returns_correct_count(self):
        self.assertEqual(len(get_last_n_months(24)), 24)
        self.assertEqual(len(get_last_n_months(1)), 1)

    def test_excludes_current_month(self):
        today = datetime.date.today()
        current_label = today.strftime("%Y-%m")
        for _, _, label in get_last_n_months(24):
            self.assertNotEqual(label, current_label)

    def test_last_entry_is_previous_month(self):
        today = datetime.date.today()
        first_of_current = today.replace(day=1)
        prev = first_of_current - datetime.timedelta(days=1)
        expected_label = prev.strftime("%Y-%m")
        _, _, label = get_last_n_months(1)[0]
        self.assertEqual(label, expected_label)

    def test_months_are_in_ascending_order(self):
        months = get_last_n_months(12)
        labels = [m[2] for m in months]
        self.assertEqual(labels, sorted(labels))

    def test_each_month_start_is_first_day(self):
        for start, _, _ in get_last_n_months(6):
            self.assertEqual(datetime.date.fromisoformat(start).day, 1)

    def test_each_month_end_is_last_day(self):
        for _, end, _ in get_last_n_months(6):
            end_date = datetime.date.fromisoformat(end)
            next_day = end_date + datetime.timedelta(days=1)
            self.assertEqual(next_day.day, 1)


# ---------------------------------------------------------------------------
# _extract_cost_row
# ---------------------------------------------------------------------------

class TestExtractCostRow(unittest.TestCase):
    def test_extracts_cost_and_currency(self):
        item = _make_cost_item("Sub A", "sub-1", 42.50)
        cost, currency, cost_str, api_status, _ = _extract_cost_row(item)
        self.assertAlmostEqual(cost, 42.50)
        self.assertEqual(currency, "USD")
        self.assertEqual(cost_str, "42.50")
        self.assertEqual(api_status, 200)

    def test_returns_zero_when_no_rows(self):
        item = _make_cost_item("Sub A", "sub-1", None, success=False, status_code=403, reason="Forbidden")
        cost, _, cost_str, _, reason = _extract_cost_row(item)
        self.assertEqual(cost, 0)
        self.assertEqual(cost_str, "0.00")
        self.assertEqual(reason, "Forbidden")

    def test_uses_fallback_reason_when_empty(self):
        item = _make_cost_item("Sub A", "sub-1", None, success=False, status_code=200, reason="")
        _, _, _, _, reason = _extract_cost_row(item)
        self.assertEqual(reason, "No cost data returned")


# ---------------------------------------------------------------------------
# _validate_environment
# ---------------------------------------------------------------------------

class TestValidateEnvironment(unittest.TestCase):
    _REQUIRED = {
        "TENANT_ID": "t", "CLIENT_ID": "c", "CLIENT_SECRET": "s",
        "ACS_CONNECTION_STRING": "a", "ACS_SENDER_EMAIL": "b", "ACS_RECIPIENT_EMAIL": "r",
    }

    def test_raises_when_vars_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                _validate_environment()
        self.assertIn("Missing environment variables", str(ctx.exception))

    def test_raises_and_names_missing_vars(self):
        with patch.dict(os.environ, {"TENANT_ID": "t"}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                _validate_environment()
        self.assertIn("CLIENT_ID", str(ctx.exception))

    def test_passes_when_all_vars_present(self):
        with patch.dict(os.environ, self._REQUIRED):
            _validate_environment()  # must not raise


# ---------------------------------------------------------------------------
# generate_csv
# ---------------------------------------------------------------------------

class TestGenerateCsv(unittest.TestCase):
    def test_totals_cost_correctly(self):
        items = [
            _make_cost_item("Sub A", "sub-1", 10.00),
            _make_cost_item("Sub B", "sub-2", 5.00),
        ]
        _, total = generate_csv(items, "01-01-2026", "01-31-2026")
        self.assertAlmostEqual(total, 15.00)

    def test_csv_contains_header_and_total_row(self):
        items = [_make_cost_item("Sub A", "sub-1", 10.00)]
        csv_content, _ = generate_csv(items, "01-01-2026", "01-31-2026")
        self.assertIn("Subscription Name", csv_content)
        self.assertIn("TOTAL", csv_content)

    def test_csv_contains_subscription_name(self):
        items = [_make_cost_item("My Subscription", "sub-1", 10.00)]
        csv_content, _ = generate_csv(items, "01-01-2026", "01-31-2026")
        self.assertIn("My Subscription", csv_content)

    def test_zero_cost_when_no_rows(self):
        items = [_make_cost_item("Sub A", "sub-1", None, success=False, status_code=403)]
        _, total = generate_csv(items, "01-01-2026", "01-31-2026")
        self.assertAlmostEqual(total, 0.0)


# ---------------------------------------------------------------------------
# generate_history_csv
# ---------------------------------------------------------------------------

class TestGenerateHistoryCsv(unittest.TestCase):
    def _make_months_data(self):
        return [
            {
                "month_label": "2026-01",
                "costs_data": [_make_cost_item("Sub A", "sub-1", 100.0)],
            },
            {
                "month_label": "2026-02",
                "costs_data": [_make_cost_item("Sub A", "sub-1", 200.0)],
            },
        ]

    def test_grand_total_is_sum_of_all_months(self):
        _, grand_total, _ = generate_history_csv(self._make_months_data())
        self.assertAlmostEqual(grand_total, 300.0)

    def test_monthly_totals_length_matches_input(self):
        _, _, monthly_totals = generate_history_csv(self._make_months_data())
        self.assertEqual(len(monthly_totals), 2)

    def test_csv_contains_month_labels_and_grand_total(self):
        csv_content, _, _ = generate_history_csv(self._make_months_data())
        self.assertIn("2026-01", csv_content)
        self.assertIn("2026-02", csv_content)
        self.assertIn("GRAND TOTAL", csv_content)

    def test_monthly_totals_values_are_correct(self):
        _, _, monthly_totals = generate_history_csv(self._make_months_data())
        self.assertAlmostEqual(monthly_totals[0]["total"], 100.0)
        self.assertAlmostEqual(monthly_totals[1]["total"], 200.0)


# ---------------------------------------------------------------------------
# HTTP endpoints (no Azure calls — validates routing and input handling)
# ---------------------------------------------------------------------------

class TestRunEmailCostReportEndpoint(unittest.TestCase):
    def _make_request(self, method="GET"):
        return func.HttpRequest(
            method=method,
            url="http://localhost/api/reports/email/run",
            params={},
            body=b"",
        )

    def test_returns_400_when_env_vars_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            response = run_email_cost_report(self._make_request())
        self.assertEqual(response.status_code, 400)


class TestRunHistoryCostReportEndpoint(unittest.TestCase):
    def _make_request(self, months=None):
        params = {"months": months} if months is not None else {}
        return func.HttpRequest(
            method="GET",
            url="http://localhost/api/reports/email/history",
            params=params,
            body=b"",
        )

    def test_rejects_non_numeric_months(self):
        response = run_history_cost_report(self._make_request(months="abc"))
        self.assertEqual(response.status_code, 400)

    def test_rejects_months_above_max(self):
        response = run_history_cost_report(self._make_request(months="61"))
        self.assertEqual(response.status_code, 400)

    def test_rejects_months_below_min(self):
        response = run_history_cost_report(self._make_request(months="0"))
        self.assertEqual(response.status_code, 400)

    def test_returns_400_when_env_vars_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            response = run_history_cost_report(self._make_request())
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
