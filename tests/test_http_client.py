import io
import logging
import unittest
import urllib.error

from keyword_scanner.config import RequestConfig
from keyword_scanner.http_client import HttpClient

CONFIG = RequestConfig(delay_seconds=1.0, max_retries=3, timeout_seconds=5, user_agent="test")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeOpener:
    """outcomes 순서대로 응답(bytes) 또는 예외를 낸다."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list = []

    def __call__(self, request, timeout):
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(outcome)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


class HttpClientTest(unittest.TestCase):
    def _client(self, outcomes: list) -> tuple[HttpClient, FakeOpener, FakeClock]:
        clock = FakeClock()
        opener = FakeOpener(outcomes)
        return HttpClient(CONFIG, opener=opener, sleep=clock.sleep, clock=clock.time), opener, clock

    def test_success_first_try(self) -> None:
        client, opener, _ = self._client([b"ok"])
        self.assertEqual(client.get("http://x"), b"ok")
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0].get_header("User-agent"), "test")

    def test_retries_then_succeeds(self) -> None:
        client, opener, _ = self._client([urllib.error.URLError("down"), TimeoutError(), b"ok"])
        self.assertEqual(client.get("http://x"), b"ok")
        self.assertEqual(len(opener.calls), 3)

    def test_gives_up_after_three_retries_and_logs(self) -> None:
        client, opener, _ = self._client([urllib.error.URLError("down")] * 4)
        with self.assertLogs("keyword_scanner.http_client", level=logging.ERROR) as logs:
            self.assertIsNone(client.get("http://x"))
        self.assertEqual(len(opener.calls), 4)  # 첫 시도 1 + 재시도 3
        self.assertIn("건너뜀", logs.output[-1])

    def test_404_not_retried(self) -> None:
        client, opener, _ = self._client([http_error(404)])
        with self.assertLogs("keyword_scanner.http_client", level=logging.ERROR):
            self.assertIsNone(client.get("http://x"))
        self.assertEqual(len(opener.calls), 1)

    def test_429_and_500_retried(self) -> None:
        client, opener, _ = self._client([http_error(429), http_error(503), b"ok"])
        self.assertEqual(client.get("http://x"), b"ok")
        self.assertEqual(len(opener.calls), 3)

    def test_waits_one_second_between_requests(self) -> None:
        client, _, clock = self._client([b"a", b"b"])
        client.get("http://x")
        clock.now += 0.3  # 응답 처리에 0.3초 걸렸다고 가정
        client.get("http://y")
        self.assertEqual(len(clock.sleeps), 1)
        self.assertAlmostEqual(clock.sleeps[0], 0.7)

    def test_no_wait_before_first_request(self) -> None:
        client, _, clock = self._client([b"a"])
        client.get("http://x")
        self.assertEqual(clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
