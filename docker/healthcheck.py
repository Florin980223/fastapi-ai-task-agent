"""Container HEALTHCHECK script.

Stdlib-only (avoids installing curl/wget into the slim image just for
this). Hits GET /health, which is deliberately public - no X-API-Key
required, see app/main.py and app/services/auth.py.

check_health() is a plain function (not folded into the CLI block) so
it can be unit-tested directly - see tests/test_docker_healthcheck.py -
without Docker, a real server, or a real network call.
"""

import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8000/health"
TIMEOUT_SECONDS = 2.0


def check_health(url: str = URL, timeout: float = TIMEOUT_SECONDS) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


if __name__ == "__main__":
    sys.exit(0 if check_health() else 1)
