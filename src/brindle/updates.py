"""Best-effort daily PyPI checks, shared by session launches and doctor."""

import fcntl
import json
import os
import time
from importlib.metadata import distribution
from urllib.request import urlopen

from packaging.version import Version

from brindle import __version__, airgap
from brindle.config import brindle_home


def newer_version(*, refresh: bool = True) -> str | None:
    """Return a newer release; status-bar reads use only the cache.

    A failed request also counts as today's check. The lock prevents two
    simultaneous launches from both contacting PyPI; never wait on it.
    Direct URL installs (including editable checkouts) aren't PyPI tools.
    """
    if "BRINDLE_NO_UPDATE_CHECK" in os.environ or airgap.enabled():
        return None
    try:
        if distribution("brindle").read_text("direct_url.json"):
            return None
        current = Version(__version__)
        home = brindle_home()
        path = home / "update-check.json"
        if refresh:
            home.mkdir(parents=True, exist_ok=True)
            with (home / "update-check.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return newer_version(refresh=False)
                cached = _read(path)
                now = time.time()
                if not 0 <= now - float(cached.get("checked_at", 0)) < 86400:
                    latest = cached.get("latest")
                    try:
                        with urlopen("https://pypi.org/pypi/brindle/json", timeout=2) as response:
                            latest = str(Version(json.load(response)["info"]["version"]))
                    except Exception:  # noqa: BLE001 -- offline or malformed: keep the last result
                        pass
                    cached = {"checked_at": now, "latest": latest}
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(cached))
                    temporary.replace(path)
        else:
            cached = _read(path)
        latest = cached.get("latest")
        return latest if latest and Version(latest) > current else None
    except Exception:  # noqa: BLE001 -- updates must never prevent a session or doctor
        return None


def _read(path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def notice(*, refresh: bool = False, compact: bool = False) -> str:
    latest = newer_version(refresh=refresh)
    if not latest:
        return ""
    prefix = "" if compact else f"brindle {__version__} "
    return f"{prefix}→ {latest} available"
