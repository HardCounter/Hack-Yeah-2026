"""Small browser-origin and request-budget guards shared by the HTTP services."""
from collections import deque
import os
import time
from urllib.parse import urlsplit


def positive_int(environment, default):
    try:
        return max(1, int(os.environ.get(environment, str(default))))
    except ValueError:
        return default


def browser_origin_allowed(request, environment):
    origin = request.headers.get("origin")
    if not origin:
        # Fetch Metadata also protects mutations that omit Origin in browser requests.
        return request.headers.get("sec-fetch-site") not in ("cross-site", "same-site")
    allowed = {value.strip().rstrip("/") for value in os.environ.get(environment, "").split(",") if value.strip()}
    if origin in allowed:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (parsed.scheme == request.url.scheme and parsed.netloc == request.headers.get("host")
            and not parsed.path and not parsed.query and not parsed.fragment and not parsed.username)


class RequestBudget:
    """A bounded, process-local sliding window; no unbounded per-client state."""
    def __init__(self, environment, default, seconds=60):
        self.environment, self.default, self.seconds = environment, default, seconds
        self.requests = deque()

    def take(self):
        limit = positive_int(self.environment, self.default)
        now = time.monotonic()
        while self.requests and self.requests[0] <= now - self.seconds:
            self.requests.popleft()
        if len(self.requests) >= limit:
            return False
        self.requests.append(now)
        return True
