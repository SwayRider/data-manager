import re

import requests

BASE_URL = "https://download.geofabrik.de"
PATH_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*(/[a-z0-9]+(-[a-z0-9]+)*)*$")


def is_valid_path(path: str) -> bool:
    """Format check only, e.g. 'europe/belgium' or 'europe/germany/bayern'."""
    return bool(PATH_RE.match(path))


def pbf_url(path: str) -> str:
    return f"{BASE_URL}/{path}-latest.osm.pbf"


def poly_url(path: str) -> str:
    return f"{BASE_URL}/{path}.poly"


def extract_exists(path: str, timeout: float = 10) -> bool:
    """Whether Geofabrik publishes this extract.

    Checks the small `.poly` file rather than `-latest.osm.pbf`: the pbf URL
    answers with redirects even for paths that don't exist, whereas `.poly`
    is a plain 200 for every real extract and a redirect/404 otherwise.
    Raises requests.RequestException if Geofabrik is unreachable.
    """
    response = requests.head(poly_url(path), allow_redirects=False, timeout=timeout)
    return response.status_code == 200


def _get_text(url: str, timeout: float, attempts: int = 3) -> str:
    import time

    from datamanager.errors import DownloadError

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code >= 400:
                raise DownloadError(f"{url} answered HTTP {response.status_code}", url=url)
            return response.text
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == attempts:
                raise DownloadError(f"Could not read {url}: {exc}", url=url) from exc
            time.sleep(2.0 * attempt)
        except requests.RequestException as exc:
            raise DownloadError(f"Could not read {url}: {exc}", url=url) from exc


def newest_dated_pbf_url(base: str, path: str, timeout: float = 60) -> str:
    """URL of the newest dated extract (`<name>-YYMMDD.osm.pbf`) of a path.

    Fallback for when Geofabrik's `-latest` alias misbehaves; the dated files are stable URLs. The dated
    files are listed on the region's page (`<path>.html`; the plain directory listing now redirects to a
    page without them), so that is read first, then the directory listing. Raises DownloadError when
    neither lists one."""
    from datamanager.errors import DownloadError

    parent, _, name = path.rpartition("/")
    folder = f"{base.rstrip('/')}/{parent}/" if parent else f"{base.rstrip('/')}/"
    pattern = rf'href="(?:[^"]*/)?{re.escape(name)}-(\d{{6}})\.osm\.pbf"'
    last_error, read_any = None, False
    for page in (f"{folder}{name}.html", folder):
        try:
            dates = re.findall(pattern, _get_text(page, timeout))
        except DownloadError as exc:
            last_error = exc
            continue
        read_any = True
        if dates:
            return f"{folder}{name}-{max(dates)}.osm.pbf"
    if not read_any and last_error is not None:
        raise last_error
    raise DownloadError(f"No dated extract for {path} on {folder}{name}.html or the directory listing", url=folder)
