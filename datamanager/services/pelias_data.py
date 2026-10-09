"""The data a Pelias import of one region reads, the `pelias.json` it runs with and the importer calls.

Everything is laid out below one directory per region (`Layout`) in the shape the importers expect, from approved inputs:

    wof/sqlite/whosonfirst-data-{admin,postalcode}-<cc>-latest.db   (WOF bundles unpacked, or the patched database)
    geonames/<cc>/<CC>.zip                                          (re-zipped: the original is not always readable)
    openaddresses/<source>.geojson                                  (gunzipped job output, as the importer's downloader does)
    osm/<slug>.osm.pbf (link), polylines/polylines.0sv (gunzipped: the importer does not)

The importers run from the cloned repositories (`pelias_build.repo_dir`) with `PELIAS_CONFIG` set; a configuration has no
token because everything is downloaded already."""
import copy
import gzip
import json
import shutil
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from datamanager.services import wof_patch
from datamanager.services.valhalla_build import run as run_command

IMPORTERS = ("schema", "whosonfirst", "geonames", "openaddresses", "openstreetmap", "polylines", "csv-importer", "transit")
OPTIONAL_IMPORTERS = ("csv-importer", "transit")  # their failure is a warning: Overture and GTFS never fail a region
OVERTURE_FILES = {"places": "overture-places.csv", "addresses": "overture-addresses.csv"}
INTERPOLATION_PORT = 4300
PROD_WOF_PATH = "/data/whosonfirst"
PLACEHOLDER_URL = "http://pelias-placeholder:3000"
LIBPOSTAL_URL = "http://pelias-libpostal:4400"


@dataclass
class CountryData:
    """Files of one country: `wof_admin` is a plain SQLite (patched asset) or a bz2 bundle (`wof_admin_packed`)."""

    iso2: str
    wof_code: str
    wof_admin: Path
    wof_admin_packed: bool
    geonames: Path
    wof_postal: Path | None = None

    @property
    def geonames_code(self) -> str:
        return self.iso2.split("-")[-1].upper()


@dataclass
class Layout:
    root: Path
    slug: str

    @property
    def wof(self) -> Path:
        return self.root / "wof"

    @property
    def wof_sqlite(self) -> Path:
        return self.wof / "sqlite"

    def geonames(self, cc: str) -> Path:
        return self.root / "geonames" / cc.lower()

    @property
    def openaddresses(self) -> Path:
        return self.root / "openaddresses"

    @property
    def osm(self) -> Path:
        return self.root / "osm"

    @property
    def leveldb(self) -> Path:
        return self.root / "leveldb"

    @property
    def polylines(self) -> Path:
        return self.root / "polylines"

    @property
    def csv(self) -> Path:
        return self.root / "csv"

    @property
    def transit(self) -> Path:
        return self.root / "transit"

    @property
    def configs(self) -> Path:
        return self.root / "config"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def pbf_name(self) -> str:
        return f"{self.slug}.osm.pbf"


# ---- laying out the input data ------------------------------------------------------------------------------------

def _link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    target.symlink_to(Path(source).resolve())


def rezip(source: Path, target: Path) -> Path:
    """The same content in a fresh zip: the importer cannot always read the original GeoNames zip (the legacy did this too)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".part")
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as out:
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as src, out.open(info.filename, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    temp.replace(target)
    return target


def gunzip(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    with gzip.open(source, "rb") as src, open(temp, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    temp.replace(target)
    return target


def prepare_gtfs(layout: Layout, feeds: list[tuple[str, Path]]) -> list[dict]:
    """Extract `stops.txt` of every GTFS zip to `transit/<name>-stops.txt` (the transit importer's own downloader is not used)
    and return the `imports.transit.feeds` entries for them."""
    found = []
    layout.transit.mkdir(parents=True, exist_ok=True)
    for name, file in feeds:
        filename = f"{name}-stops.txt"
        with zipfile.ZipFile(file) as archive, archive.open("stops.txt") as src, open(layout.transit / filename, "wb") as dst:
            shutil.copyfileobj(src, dst)
        found.append({"layerId": "stops", "filename": filename, "agencyId": name, "agencyName": name, "layerName": "stop"})
    return found


def prepare(layout: Layout, countries: list[CountryData], openaddresses: dict[str, Path], pbf: Path, polylines: Path,
            overture: dict[str, Path] | None = None) -> dict:
    """Write every input where the importers read it; returns counts for the report."""
    layout.wof_sqlite.mkdir(parents=True, exist_ok=True)
    for country in countries:
        code = country.wof_code
        target = layout.wof_sqlite / f"whosonfirst-data-admin-{code}-latest.db"
        if country.wof_admin_packed:
            wof_patch.unpack_wof(country.wof_admin, target)
        else:
            _link(country.wof_admin, target)
        if country.wof_postal is not None:
            wof_patch.unpack_wof(country.wof_postal, layout.wof_sqlite / f"whosonfirst-data-postalcode-{code}-latest.db")
        rezip(country.geonames, layout.geonames(country.geonames_code) / f"{country.geonames_code}.zip")
    for source, file in openaddresses.items():
        gunzip(file, layout.openaddresses / f"{source}.geojson")
    for theme, file in (overture or {}).items():
        _link(file, layout.csv / OVERTURE_FILES[theme])
    _link(pbf, layout.osm / layout.pbf_name)
    gunzip(polylines, layout.polylines / "polylines.0sv")  # the polylines importer reads the file as plain text, it does not gunzip
    for directory in (layout.leveldb, layout.csv, layout.transit, layout.configs, layout.logs):
        directory.mkdir(parents=True, exist_ok=True)
    return {"countries": len(countries), "openaddresses_files": len(openaddresses), "overture_files": len(overture or {})}


# ---- configuration ------------------------------------------------------------------------------------------------

def render_config(layout: Layout, *, index: str, es_host: str, es_port: int, wof_codes: list[str], openaddresses: list[str],
                  prod: bool = False, csv_files: list[str] | None = None, transit_feeds: list[dict] | None = None) -> dict:
    """The legacy `pelias-load.json`, one dict. `prod` is the configuration the deployed API and PIP service use: the
    docker service host name and the WOF path inside the PIP container."""
    wof_path = PROD_WOF_PATH if prod else str(layout.wof)
    return {
        "esclient": {"apiVersion": "7.5", "keepAlive": True, "requestTimeout": "120000",
                     "hosts": [{"env": "development", "protocol": "http", "host": es_host, "port": es_port}],
                     "log": [{"type": "stdio", "json": False, "level": ["error", "warning"]}]},
        "elasticsearch": {"settings": {"index": {"number_of_replicas": "0", "number_of_shards": "1", "refresh_interval": "1m"}}},
        # importers push nothing to the interpolation service; the deployed API asks it through api.services.interpolation
        "interpolation": {"client": {"adapter": "null"}},
        "dbclient": {"statFrequency": 10000, "batchSize": 500},
        "api": {
            "accessLog": "common", "indexName": index,
            "services": {"placeholder": {"url": PLACEHOLDER_URL}, "libpostal": {"url": LIBPOSTAL_URL},
                         "pip": {"url": f"http://pelias-{layout.slug}-pip:3102", "timeout": 1000, "retries": 2},
                         **({"interpolation": {"url": f"http://pelias-{layout.slug}-interpolation:{INTERPOLATION_PORT}"}} if prod else {})},
            "targets": {
                "auto_discover": True,
                "canonical_sources": ["whosonfirst", "openstreetmap", "openaddresses", "geonames"],
                "layers_by_source": {
                    "openstreetmap": ["address", "venue", "street"],
                    "openaddresses": ["address"],
                    "geonames": ["country", "macroregion", "region", "county", "localadmin", "locality", "borough", "neighbourhood", "venue"],
                    "whosonfirst": ["continent", "empire", "country", "dependency", "macroregion", "region", "locality", "localadmin",
                                    "macrocounty", "county", "macrohood", "borough", "neighbourhood", "microhood", "disputed", "venue",
                                    "postalcode", "ocean", "marinearea"]},
                "source_aliases": {"osm": ["openstreetmap"], "oa": ["openaddresses"], "gn": ["geonames"], "wof": ["whosonfirst"]},
                "layer_aliases": {"coarse": ["continent", "empire", "country", "dependency", "macroregion", "region", "locality", "localadmin",
                                             "macrocounty", "county", "macrohood", "borough", "neighbourhood", "microhood", "disputed",
                                             "postalcode", "ocean", "marinearea"]}}},
        "schema": {"indexName": index},
        "logger": {"level": "info", "timestamp": True, "colorize": False},
        "acceptance-tests": {"endpoints": {"local": "http://localhost:3100/v1/"}},
        "imports": {
            "adminLookup": {"enabled": True, "maxConcurrentRequests": 100, "usePostalCities": True},
            "blacklist": {"files": []},
            "csv": {"datapath": str(layout.csv), "files": list(OVERTURE_FILES.values()) if csv_files is None else csv_files},
            "geonames": {"datapath": str(layout.root / "geonames"), "countryCode": "ALL"},
            "openstreetmap": {"datapath": str(layout.osm), "leveldbpath": str(layout.leveldb), "removeDisusedVenues": True,
                              "import": [{"filename": layout.pbf_name}]},
            "openaddresses": {"datapath": str(layout.openaddresses), "files": openaddresses},
            "polyline": {"datapath": str(layout.polylines), "files": ["polylines.0sv"]},
            "whosonfirst": {"datapath": wof_path, "importPostalcodes": True, "countryCode": wof_codes},
            "transit": {"datapath": str(layout.transit), "feeds": transit_feeds or []},
        },
    }


def geonames_config(base: dict, layout: Layout, cc: str) -> dict:
    """The configuration for one GeoNames importer run: that country's directory and country code."""
    config = copy.deepcopy(base)
    config["imports"]["geonames"] = {"datapath": str(layout.geonames(cc)), "countryCode": cc.upper()}
    return config


def write_config(config: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


# ---- running an importer ------------------------------------------------------------------------------------------

def importer_command(name: str) -> list[str]:
    return ["./bin/create_index"] if name == "schema" else ["./bin/start"]


def run_importer(name: str, repo: Path, config_path: Path, log_path: Path, label: str | None = None) -> None:
    """One importer of the cloned repositories with `PELIAS_CONFIG`; its output is appended to the region's log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"== {label or name}: {' '.join(importer_command(name))} (PELIAS_CONFIG={config_path})\n")
        log.flush()
        run_command(importer_command(name), cwd=repo, log=log, env={"PELIAS_CONFIG": str(config_path)})


# ---- packing the results ------------------------------------------------------------------------------------------

def tar_directory(source: Path, target: Path, gz: bool = False) -> Path:
    """`source`'s content as a tar (the snapshot repository) or tar.gz (the WOF directory), written atomically."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    with tarfile.open(temp, "w:gz" if gz else "w", dereference=True) as archive:  # the WOF directory holds links to the assets
        for child in sorted(Path(source).iterdir()):
            archive.add(child, arcname=child.name)
    temp.replace(target)
    return target


def directory_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file() and not f.is_symlink())
