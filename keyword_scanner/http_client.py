"""요청 간격(차단 방지) + 재시도 + 실패 로그를 한 곳에서 처리하는 HTTP 클라이언트."""

import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .config import RequestConfig

log = logging.getLogger(__name__)

Opener = Callable[..., object]


class HttpClient:
    def __init__(
        self,
        config: RequestConfig,
        opener: Opener = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        wait = self._config.delay_seconds - (self._clock() - self._last_request_at)
        if wait > 0:
            self._sleep(wait)

    def get(self, url: str, headers: dict[str, str] | None = None) -> bytes | None:
        """본문을 돌려준다. 재시도까지 모두 실패하면 로그를 남기고 None."""
        request_headers = {"User-Agent": self._config.user_agent, **(headers or {})}
        attempts = 1 + self._config.max_retries
        for attempt in range(1, attempts + 1):
            self._throttle()
            self._last_request_at = self._clock()
            try:
                request = urllib.request.Request(url, headers=request_headers)
                with self._opener(request, timeout=self._config.timeout_seconds) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                error.close()  # 오류 응답도 연결을 쥐고 있으므로 닫는다
                # 404 같은 클라이언트 오류는 다시 보내도 같으므로 바로 포기 (429 너무 많은 요청은 재시도)
                if 400 <= error.code < 500 and error.code != 429:
                    log.error("건너뜀 (HTTP %s): %s", error.code, url)
                    return None
                log.warning("실패 %d/%d (HTTP %s): %s", attempt, attempts, error.code, url)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                log.warning("실패 %d/%d (%s): %s", attempt, attempts, error, url)
            if attempt < attempts:
                # 실패할수록 조금 더 쉬었다가 다시 시도
                self._sleep(self._config.delay_seconds * attempt)
        log.error("건너뜀 (%d번 시도 모두 실패): %s", attempts, url)
        return None
