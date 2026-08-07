"""Compare the local read-only catalog with the current Tushare documentation."""

from concurrent.futures import ThreadPoolExecutor
from html import unescape
import re
from time import sleep
from urllib.request import Request, urlopen

from china_a_share.registry import READ_ONLY_API_NAMES


DOCUMENTATION_URL = "https://tushare.pro/document/2"
DOCUMENT_PATH_PATTERN = re.compile(
    r'<li\s*><a href="(?P<path>/document/2\?doc_id=\d+)">'
)
API_NAME_PATTERNS = (
    re.compile(r"接口名称\s*[：:]\s*(?P<name>[A-Za-z][A-Za-z0-9_]*)"),
    re.compile(r"接口\s*[：:]\s*(?P<name>[A-Za-z][A-Za-z0-9_]*)"),
    re.compile(r"API\s*[：:]\s*(?P<name>[A-Za-z][A-Za-z0-9_]*)"),
)
EXCLUDED_WRITE_OPERATIONS = {"p_delete", "p_save"}
REQUEST_TIMEOUT_SECONDS = 60
MAX_AUDIT_WORKERS = 8
MAX_FETCH_ATTEMPTS = 3
FETCH_RETRY_DELAY_SECONDS = 1


def main() -> None:
    """Fetch official documentation and fail when a read-only API is unconnected."""
    index = _fetch(DOCUMENTATION_URL)
    paths = sorted(set(DOCUMENT_PATH_PATTERN.findall(index)))
    with ThreadPoolExecutor(max_workers=MAX_AUDIT_WORKERS) as executor:
        documented = {
            operation
            for operation in executor.map(_extract_operation, paths)
            if operation
        }

    connected = set(READ_ONLY_API_NAMES)
    missing = sorted(documented - connected - EXCLUDED_WRITE_OPERATIONS)
    stale = sorted(connected - documented)
    print(
        "Tushare catalog audit: "
        f"documented_api_operations={len(documented)}, "
        f"connected_read_only_operations={len(connected)}, "
        f"excluded_write_operations={len(EXCLUDED_WRITE_OPERATIONS)}"
    )
    if missing or stale:
        raise SystemExit(
            "Tushare catalog coverage mismatch: "
            f"missing={missing}, stale={stale}"
        )


def _extract_operation(path: str) -> str:
    """Extract one documented API name, or empty text for non-API data pages."""
    page = re.sub(r"<[^>]+>", " ", _fetch(f"https://tushare.pro{path}"))
    text = unescape(re.sub(r"\s+", " ", page))
    for pattern in API_NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group("name")
    return ""


def _fetch(url: str) -> str:
    """Return one UTF-8 documentation page through a bounded read-only request."""
    request = Request(url, headers={"User-Agent": "china-a-share-catalog-audit/1"})
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.read().decode("utf-8")
        except OSError:
            if attempt + 1 == MAX_FETCH_ATTEMPTS:
                raise
            sleep(FETCH_RETRY_DELAY_SECONDS)
    raise RuntimeError("Tushare documentation fetch exhausted without a result.")


if __name__ == "__main__":
    main()
