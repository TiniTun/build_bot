"""Mapping of Google API ``HttpError`` failures to provider exceptions."""

import json
import unittest

from provider.external_errors import (
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
)
from provider.google_errors import map_google_http_error
from tools.base import ToolErrorCode
from tools.external_support import provider_exception_to_result


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Forbidden"


def _http_error(status: int, reason: str | None = None):
    from googleapiclient.errors import HttpError

    if reason is None:
        return HttpError(_Resp(status), b"{}")
    body = {
        "error": {
            "code": status,
            "message": "denied",
            "errors": [{"domain": "usageLimits", "reason": reason, "message": "x"}],
        }
    }
    return HttpError(_Resp(status), json.dumps(body).encode())


class MapGoogleHttpErrorTests(unittest.TestCase):
    def test_403_rate_limit_reasons_are_rate_limited(self) -> None:
        for reason in (
            "rateLimitExceeded",
            "userRateLimitExceeded",
            "quotaExceeded",
            "dailyLimitExceeded",
        ):
            with self.subTest(reason=reason), self.assertLogs("provider.google_errors", "WARNING"):
                mapped = map_google_http_error(_http_error(403, reason), "calendar")
                self.assertIsInstance(mapped, ProviderRateLimitError)

    def test_429_is_rate_limited(self) -> None:
        with self.assertLogs("provider.google_errors", "WARNING"):
            mapped = map_google_http_error(_http_error(429), "calendar")
        self.assertIsInstance(mapped, ProviderRateLimitError)

    def test_real_permission_403_stays_permission(self) -> None:
        with self.assertLogs("provider.google_errors", "WARNING"):
            mapped = map_google_http_error(_http_error(403, "insufficientPermissions"), "calendar")
        self.assertIsInstance(mapped, ProviderPermissionError)

    def test_403_without_body_stays_permission(self) -> None:
        with self.assertLogs("provider.google_errors", "WARNING"):
            mapped = map_google_http_error(_http_error(403), "gmail")
        self.assertIsInstance(mapped, ProviderPermissionError)

    def test_log_names_service_status_and_reason(self) -> None:
        with self.assertLogs("provider.google_errors", "WARNING") as logs:
            map_google_http_error(_http_error(403, "rateLimitExceeded"), "calendar")
        line = logs.output[0]
        self.assertIn("calendar", line)
        self.assertIn("403", line)
        self.assertIn("rateLimitExceeded", line)

    def test_404_is_not_found(self) -> None:
        with self.assertLogs("provider.google_errors", "WARNING"):
            mapped = map_google_http_error(_http_error(404), "gmail")
        self.assertIsInstance(mapped, ProviderNotFoundError)

    def test_non_http_error_passes_through(self) -> None:
        exc = RuntimeError("boom")
        self.assertIs(map_google_http_error(exc, "calendar"), exc)


class RateLimitToolResultTests(unittest.TestCase):
    def test_rate_limit_is_retryable_rate_limited(self) -> None:
        result = provider_exception_to_result(ProviderRateLimitError("slow down"))
        payload = json.loads(result.to_tool_content())
        self.assertEqual(payload["error"]["code"], ToolErrorCode.RATE_LIMITED.value)
        self.assertTrue(payload["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
