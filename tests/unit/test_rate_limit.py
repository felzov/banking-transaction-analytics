"""The token bucket and client keys of the rate limit (ADR 0010), with a fake clock."""

import pytest

from transaction_analytics.api.rate_limit import TokenBucket, client_key


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _allowed(bucket: TokenBucket, client: str, times: int) -> list[bool]:
    return [bucket.take(client).allowed for _ in range(times)]


def test_a_burst_is_allowed_then_refused(clock: FakeClock) -> None:
    bucket = TokenBucket(per_minute=60, burst=3, clock=clock)

    assert _allowed(bucket, "a", 4) == [True, True, True, False]


def test_tokens_come_back_at_the_rate(clock: FakeClock) -> None:
    bucket = TokenBucket(per_minute=60, burst=2, clock=clock)  # one per second
    assert _allowed(bucket, "a", 3) == [True, True, False]

    clock.now += 1
    assert _allowed(bucket, "a", 2) == [True, False]

    clock.now += 2
    assert _allowed(bucket, "a", 3) == [True, True, False]


def test_an_idle_client_gets_no_more_than_the_burst(clock: FakeClock) -> None:
    bucket = TokenBucket(per_minute=60, burst=2, clock=clock)
    bucket.take("a")

    clock.now += 3600

    assert _allowed(bucket, "a", 3) == [True, True, False]


def test_refused_requests_are_not_counted(clock: FakeClock) -> None:
    # Otherwise a client that keeps retrying would never get through again.
    bucket = TokenBucket(per_minute=60, burst=1, clock=clock)
    assert _allowed(bucket, "a", 6) == [True] + [False] * 5

    clock.now += 1

    assert bucket.take("a").allowed


def test_retry_after_is_the_wait_for_one_token_in_whole_seconds(clock: FakeClock) -> None:
    bucket = TokenBucket(per_minute=30, burst=1, clock=clock)  # one every 2 s
    assert bucket.take("a").retry_after_seconds == 0

    assert bucket.take("a").retry_after_seconds == 2
    clock.now += 1.5
    assert bucket.take("a").retry_after_seconds == 1  # 0.5 s left, rounded up


def test_clients_have_separate_buckets(clock: FakeClock) -> None:
    bucket = TokenBucket(per_minute=60, burst=1, clock=clock)

    assert _allowed(bucket, "a", 2) == [True, False]
    assert bucket.take("b").allowed


def test_the_least_recently_seen_client_is_dropped_first(clock: FakeClock) -> None:
    bucket = TokenBucket(per_minute=60, burst=1, max_clients=2, clock=clock)
    bucket.take("a")
    bucket.take("b")
    assert not bucket.take("a").allowed  # "a" is now the most recently seen

    bucket.take("c")  # one client too many: "b" goes

    assert len(bucket) == 2
    assert not bucket.take("a").allowed  # kept, still empty
    assert bucket.take("b").allowed  # dropped, so it starts afresh


@pytest.mark.parametrize(
    "limits",
    [
        {"per_minute": 0, "burst": 1},
        {"per_minute": 1, "burst": 0},
        {"per_minute": 1, "burst": 1, "max_clients": 0},
    ],
)
def test_limits_must_be_positive(limits: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        TokenBucket(**limits)


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        # One host usually holds a whole /64; per address it could start afresh on every request.
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),  # IPv4 on a dual-stack socket
        ("testclient", "testclient"),  # not an IP address: as is
        (None, "unknown"),  # the server reported no client
    ],
)
def test_client_key(host: str | None, key: str) -> None:
    assert client_key(host) == key
