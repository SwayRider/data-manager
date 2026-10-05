"""Overture Maps places and addresses of a region as the CSV files the Pelias csv-importer reads.

`overturemaps download --bbox ... -f geojsonseq` writes one GeoJSON feature per line (the plain `geojson` format wraps them in
a FeatureCollection with trailing commas, which the legacy line parser silently dropped). The 2026-09 schema has no
`categories` any more (`taxonomy.primary`), no address `street`/`number` on places (only a `freeform` line) and keeps the
locality of an address in `address_levels`; the conversions below follow that."""
import csv
import json
import re
import subprocess
from pathlib import Path

PLACE_COLUMNS = ["id", "name", "lat", "lon", "layer", "source", "category", "housenumber", "street", "postcode", "city", "country"]
ADDRESS_COLUMNS = ["id", "lat", "lon", "layer", "source", "housenumber", "street", "postcode", "city", "country"]
THEMES = {"places": "place", "addresses": "address"}
_NUMBER_FIRST = re.compile(r"^(\d+[\w/.-]*)\s+(.+)$")
_NUMBER_LAST = re.compile(r"^(.+?)[,\s]+(\d+[\w/.-]*)$")


class OvertureError(Exception):
    pass


def latest_release(binary: str, timeout: float = 120) -> str:
    try:
        proc = subprocess.run([binary, "releases", "latest"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OvertureError(f"overturemaps releases latest: {exc}") from exc
    release = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    if proc.returncode != 0 or not release:
        raise OvertureError("overturemaps releases latest failed: " + (proc.stderr or proc.stdout).strip()[-200:])
    return release


def download(binary: str, bbox: tuple, theme: str, release: str, target: Path, timeout: float = 6 * 3600) -> None:
    """One theme of the bbox as geojsonseq into `target`; the CLI's `.state` side file is removed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    args = [binary, "download", "--bbox", ",".join(f"{v:.6f}" for v in bbox), "-f", "geojsonseq", "--type", theme, "-r", release, "-o", str(target)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OvertureError(f"overturemaps download {theme}: {exc}") from exc
    Path(str(target) + ".state").unlink(missing_ok=True)
    if proc.returncode != 0:
        raise OvertureError(f"overturemaps download {theme} failed (exit {proc.returncode}): " + proc.stderr.strip()[-200:])


def split_freeform(text: str) -> tuple[str, str]:
    """(housenumber, street) from an address line such as `44 Av. Pasteur` or `Rue Zithe 12`; ('', '') when it has no number."""
    text = (text or "").strip()
    found = _NUMBER_FIRST.match(text)
    if found:
        return found.group(1), found.group(2).strip()
    found = _NUMBER_LAST.match(text)
    if found:
        return found.group(2), found.group(1).strip(" ,")
    return "", ""


def _features(source: Path):
    with open(source, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            try:
                feature = json.loads(line)
                lon, lat = feature["geometry"]["coordinates"][:2]
            except (ValueError, KeyError, TypeError):
                continue
            yield feature, lon, lat


def places_to_csv(source: Path, target: Path, countries: set[str] | None = None) -> dict:
    """Places with a name, as venues; `countries` (upper-case ISO2) drops places of other countries (unknown country is kept)."""
    rows = skipped = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=PLACE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for feature, lon, lat in _features(source):
            props = feature.get("properties") or {}
            name = (props.get("names") or {}).get("primary") or ""
            address = (props.get("addresses") or [{}])[0] or {}
            country = str(address.get("country") or "").upper()
            if not name or (countries and country and country not in countries):
                skipped += 1
                continue
            number, street = split_freeform(address.get("freeform") or "")
            category = (props.get("taxonomy") or {}).get("primary") or (props.get("categories") or {}).get("primary") or props.get("basic_category") or ""
            writer.writerow({"id": feature.get("id") or props.get("id") or "", "name": name, "lat": lat, "lon": lon, "layer": "venue", "source": "overture",
                             "category": category, "housenumber": number, "street": street, "postcode": address.get("postcode") or "",
                             "city": address.get("locality") or "", "country": country})
            rows += 1
    return {"rows": rows, "skipped": skipped}


def addresses_to_csv(source: Path, target: Path, countries: set[str] | None = None) -> dict:
    """Addresses with a street and a house number (the csv-importer's address layer needs both to be useful)."""
    rows = skipped = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=ADDRESS_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for feature, lon, lat in _features(source):
            props = feature.get("properties") or {}
            country = str(props.get("country") or "").upper()
            street, number = str(props.get("street") or "").strip(), str(props.get("number") or "").strip()
            if not street or not number or (countries and country and country not in countries):
                skipped += 1
                continue
            levels = props.get("address_levels") or []
            city = (levels[-1] or {}).get("value") if levels else ""
            writer.writerow({"id": feature.get("id") or props.get("id") or "", "lat": lat, "lon": lon, "layer": "address", "source": "overture",
                             "housenumber": number, "street": street, "postcode": props.get("postcode") or "", "city": city or "", "country": country})
            rows += 1
    return {"rows": rows, "skipped": skipped}


CONVERTERS = {"places": places_to_csv, "addresses": addresses_to_csv}
