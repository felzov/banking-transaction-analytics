"""Request rate limit per client address (ADR 0010).

A token bucket per client: up to `burst` requests at once, refilled at `per_minute`. A request over
the limit gets 429 with Retry-After before its body is read or its token is looked up: beyond the
limit, a flood costs no database query. Every route counts, the probes included: an exempt route
would let one address reach the app, and /ready's queries, without limit. A refused request still
costs the event loop its parsing and a 429, so a flood is capped in what it reaches, not in CPU.

- The buckets live in process memory: the limit is per server process and resets on restart. Run
  one worker (README, "Running on a server").
- The client is the address uvicorn reports. Behind a proxy that is the proxy's own, unless uvicorn
  trusts it (FORWARDED_ALLOW_IPS) and takes the client from X-Forwarded-For. Anyone else's
  X-Forwarded-For is ignored, so a client cannot pick itself a fresh bucket.
- IPv6 clients are limited per /64: a host usually has a whole /64 and could otherwise use a new
  address for every request.
- At most MAX_CLIENTS buckets; the least recently seen is dropped first, so memory stays bounded. A
  client with more addresses than that is not limited; that needs a limit in front of the server.
"""

import ipaddress
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

from transaction_analytics.api.errors import error_response

MAX_CLIENTS = 10_000


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    # Whole seconds until the client may send one more request; 0 when allowed.
    retry_after_seconds: int


class TokenBucket:
    """Not thread-safe; it is used from the event loop only, and take() never awaits."""

    def __init__(
        self,
        *,
        per_minute: int,
        burst: int,
        max_clients: int = MAX_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_minute <= 0 or burst <= 0 or max_clients <= 0:
            raise ValueError("Rate, burst and client limit must be positive.")
        self._per_second = per_minute / 60
        self._burst = burst
        self._max_clients = max_clients
        self._clock = clock
        # client -> (tokens left, when they were counted); least recently seen first.
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._buckets)

    def take(self, client: str) -> Decision:
        """One request by `client`: allowed (and counted) or refused (and not counted)."""
        now = self._clock()
        tokens, counted_at = self._buckets.pop(client, (self._burst, now))
        tokens = min(self._burst, tokens + (now - counted_at) * self._per_second)
        if tokens >= 1:
            tokens -= 1
            decision = Decision(allowed=True, retry_after_seconds=0)
        else:
            wait = math.ceil((1 - tokens) / self._per_second)
            decision = Decision(allowed=False, retry_after_seconds=max(1, wait))
        self._buckets[client] = (tokens, now)
        if len(self._buckets) > self._max_clients:
            self._buckets.popitem(last=False)
        return decision


def client_key(host: str | None) -> str:
    """The bucket a client address counts against."""
    if host is None:
        return "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host  # not an IP address, e.g. a Unix socket
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Built by the lifespan from settings (main.py); None when RATE_LIMIT_PER_MINUTE is 0.
        # Absent only when no lifespan ran (tests): a server always runs it, or it has no database.
        limiter: TokenBucket | None = getattr(scope["app"].state, "rate_limiter", None)
        if limiter is not None:
            client = scope.get("client")
            decision = limiter.take(client_key(client[0] if client else None))
            if not decision.allowed:
                response = error_response(
                    429,
                    "rate_limited",
                    "Too many requests. Retry later.",
                    headers={"Retry-After": str(decision.retry_after_seconds)},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)
