"""Registry of the external tools the pipeline needs; detection lives in `services/tools.py`.

`required` tools are needed by the stages planned for the current phases (a missing one raises a
warning in the UI); optional ones are only needed later or for a subset of features. Valhalla (and later
the Pelias repos) is compiled by data-manager itself into the tools dir (`kind="built"`, Build button in Settings)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDef:
    key: str
    label: str
    needed_by: str
    binaries: tuple[str, ...] = ()  # candidates looked up on PATH; empty for kind "file"
    version_args: tuple[str, ...] = ("--version",)
    min_version: tuple[int, ...] | None = None
    required: bool = True
    apt: str | None = None  # Debian package(s)
    install_help: str = ""  # shown when there is no simple apt package
    kind: str = "binary"  # "binary" | "file" (e.g. a jar, no version check) | "built" (compiled by us, see services/valhalla_build.py) | "library" (system library, found via pkg-config)
    default_file: str | None = None  # kind "file": path relative to the data root
    pkg_config: str | None = None  # kind "library": the pkg-config module name
    data_dirs: tuple[str, ...] = ()  # kind "library": directories (or $ENV_VAR names) that must hold the library's data files
    data_marker: str = ""  # entry that proves a data directory is complete


TOOLS: tuple[ToolDef, ...] = (
    ToolDef("osmium", "osmium-tool", "OSM stage (extract, merge, tags-filter)", ("osmium",), apt="osmium-tool"),
    ToolDef("ogr2ogr", "GDAL (ogr2ogr)", "Natural Earth / GDAL steps", ("ogr2ogr",), apt="gdal-bin"),
    ToolDef("git", "git", "Valhalla / Pelias builds", ("git",), apt="git"),
    ToolDef("cmake", "cmake", "Valhalla build", ("cmake",), min_version=(3, 16), apt="cmake"),
    ToolDef("make", "make", "Valhalla build", ("make",), apt="make"),
    ToolDef("gxx", "g++", "Valhalla build", ("g++",), apt="g++ (or build-essential)"),
    ToolDef("node", "Node.js", "Pelias (the interpolation importer needs 22 or newer)", ("node", "nodejs"), min_version=(22,), apt="nodejs"),
    ToolDef("libpostal", "libpostal", "Pelias interpolation (node-postal) and address parsing", kind="library", pkg_config="libpostal",
            data_dirs=("$LIBPOSTAL_DATA_DIR", "/usr/local/share/libpostal", "/usr/share/libpostal"), data_marker="address_parser",
            install_help="libpostal is a C library with several GB of model data. As far as is known it has no Debian package: build it from source and "
                         "download its data (check `apt search libpostal` first).\n"
                         "sudo apt install build-essential curl autoconf automake libtool pkg-config\n"
                         "git clone https://github.com/openvenues/libpostal && cd libpostal\n"
                         "./bootstrap.sh\n"
                         "./configure --datadir=/usr/local/share\n"
                         "make -j4\n"
                         "sudo make install\n"
                         "sudo ldconfig\n"
                         "(the data files are fetched during `make install` or with `sudo libpostal_data download all /usr/local/share/libpostal`; "
                         "a different data directory can be given in the environment variable LIBPOSTAL_DATA_DIR of the worker.)"),
    ToolDef("npm", "npm", "Pelias", ("npm",), apt="npm"),
    ToolDef("rsync", "rsync", "Deploy", ("rsync",), apt="rsync"),
    ToolDef("ssh", "ssh", "Deploy", ("ssh",), version_args=("-V",), apt="openssh-client"),
    ToolDef("curl", "curl", "Downloads", ("curl",), required=False, apt="curl"),
    ToolDef("unzip", "unzip", "Downloads", ("unzip",), version_args=("-v",), required=False, apt="unzip"),
    ToolDef("zip", "zip", "Pelias (GeoNames archive)", ("zip",), version_args=("-v",), required=False, apt="zip"),
    ToolDef("tar", "tar", "Pelias (snapshot archives)", ("tar",), required=False, apt="tar"),
    ToolDef("overturemaps", "overturemaps CLI", "Pelias (Overture addresses)", ("overturemaps",), version_args=("--version",), required=False,
            install_help="`pipx install overturemaps` (or `pip install overturemaps` in a virtualenv), then put it on PATH or set its path here."),
    ToolDef("docker", "Docker", "Pelias stage (temporary Elasticsearch container)", ("docker",), min_version=(20,), required=False,
            install_help="docker-ce is not in Debian's default repository: follow https://docs.docker.com/engine/install/debian/ "
                         "(or `apt install docker.io` for the distribution build)."),
    ToolDef("aws", "AWS CLI", "SRTM download (s3)", ("aws",), required=False,
            install_help="`apt install awscli`, or `pip install awscli`."),
    ToolDef("pmtiles", "pmtiles CLI", "Tiles stage (Phase 7)", ("pmtiles",), version_args=("version",), required=False,
            install_help="Not in apt: download the go-pmtiles release for your platform from https://github.com/protomaps/go-pmtiles/releases "
                         "and put `pmtiles` on PATH (or set its path here)."),
    ToolDef("pelias", "Pelias importers", "Pelias stage", kind="built", required=False,
            install_help="Needs git, Node.js and npm. Press Build: the repositories of github.com/pelias (schema, whosonfirst, geonames, "
                         "openaddresses, openstreetmap, polylines, csv-importer, transit) are cloned at the configured version "
                         "(Settings → Tool versions → Pelias importers version) and installed into the data root (tools/pelias)."),
    ToolDef("valhalla", "Valhalla (compiled)", "Valhalla stage", kind="built", required=False,
            install_help="Needs git, cmake, make and g++. Press Build: the configured version (Settings → Tools → Valhalla version) "
                         "is cloned and compiled into the data root (tools/valhalla), which takes a while."),
)
BY_KEY = {t.key: t for t in TOOLS}
