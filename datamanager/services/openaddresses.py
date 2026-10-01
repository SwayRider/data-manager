import re
from concurrent.futures import ThreadPoolExecutor

import requests

BATCH_HOST = "https://batch.openaddresses.io"
MAX_WORKERS = 5


KNOWN_EXTENSIONS = re.compile(r"\.(csv|geojson|json)$", re.IGNORECASE)


def strip_extension(source: str) -> str:
    """Canonical stored form: no file extension (the Pelias importer resolves sources without one)."""
    return KNOWN_EXTENSIONS.sub("", source.strip())


def normalize(source: str) -> str:
    """OpenAddresses source id: the catalogue path without its file extension."""
    return re.sub(r"\.[^/.]+$", "", source)


def source_exists(source: str, timeout: float = 10) -> bool:
    """Whether the OpenAddresses batch API knows a job for this source.

    Same lookup Pelias' importer does before downloading (no token needed).
    Raises requests.RequestException if the API is unreachable.
    """
    response = requests.get(
        f"{BATCH_HOST}/api/data",
        params={"source": normalize(source), "layer": "addresses", "validated": "false"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return isinstance(data, list) and bool(data) and bool(data[0].get("job"))


def find_missing(sources: list[str]) -> list[str]:
    """Sources the batch API does not know, in input order."""
    if not sources:
        return []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        found = list(pool.map(source_exists, sources))
    return [s for s, ok in zip(sources, found) if not ok]
