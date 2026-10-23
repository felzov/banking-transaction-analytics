"""The systemd unit in the README ("Running on a server", ADR 0010) keeps what the app relies on."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _directives() -> dict[str, list[str]]:
    """Key -> values of the README's systemd unit, with continued lines joined."""
    blocks = re.findall(r"```ini\n(.*?)```", (ROOT / "README.md").read_text(), re.DOTALL)
    (unit,) = [block for block in blocks if "[Service]" in block]
    directives: dict[str, list[str]] = {}
    for line in unit.replace("\\\n", " ").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            directives.setdefault(key.strip(), []).append(" ".join(value.split()))
    return directives


def test_the_service_waits_for_and_retries_the_database() -> None:
    # Review fix (m1): ordered after the network only, a reboot ran the migration before a local
    # PostgreSQL was up. Restarted every 100 ms (systemd's default), five failures within ten
    # seconds hit the start limit and left the service down until someone started it by hand.
    directives = _directives()

    assert "postgresql.service" in " ".join(directives["After"])
    assert "postgresql.service" in " ".join(directives["Wants"])
    (restart_sec,) = directives["RestartSec"]
    assert int(restart_sec.removesuffix("s")) >= 5


def test_the_service_runs_one_worker_behind_a_local_proxy() -> None:
    # The rate limit is per process, and only the local proxy may name the client (ADR 0010).
    (exec_start,) = _directives()["ExecStart"]

    assert "--workers 1 " in exec_start
    assert "--host 127.0.0.1 " in exec_start
    assert "--forwarded-allow-ips 127.0.0.1 " in exec_start
