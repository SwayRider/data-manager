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
    kind: str = "binary"  # "binary" | "file" (e.g. a jar, no version check) | "built" (compiled by us, see services/valhalla_build.py)
    default_file: str | None = None  # kind "file": path relative to the data root


TOOLS: tuple[ToolDef, ...] = (
    ToolDef("osmium", "osmium-tool", "OSM stage (extract, merge, tags-filter)", ("osmium",), apt="osmium-tool"),
    ToolDef("ogr2ogr", "GDAL (ogr2ogr)", "Natural Earth / GDAL steps", ("ogr2ogr",), apt="gdal-bin"),
    ToolDef("git", "git", "Valhalla / Pelias builds", ("git",), apt="git"),
    ToolDef("cmake", "cmake", "Valhalla build", ("cmake",), min_version=(3, 16), apt="cmake"),
    ToolDef("make", "make", "Valhalla build", ("make",), apt="make"),
    ToolDef("gxx", "g++", "Valhalla build", ("g++",), apt="g++ (or build-essential)"),
    ToolDef("node", "Node.js", "Pelias", ("node", "nodejs"), min_version=(18,), apt="nodejs"),
    ToolDef("npm", "npm", "Pelias", ("npm",), apt="npm"),
    ToolDef("rsync", "rsync", "Deploy", ("rsync",), apt="rsync"),
    ToolDef("ssh", "ssh", "Deploy", ("ssh",), version_args=("-V",), apt="openssh-client"),
    ToolDef("curl", "curl", "Downloads", ("curl",), required=False, apt="curl"),
    ToolDef("unzip", "unzip", "Downloads", ("unzip",), version_args=("-v",), required=False, apt="unzip"),
    ToolDef("docker", "Docker", "Elasticsearch / Pelias containers", ("docker",), required=False,
            install_help="docker-ce is not in Debian's default repository: follow https://docs.docker.com/engine/install/debian/ "
                         "(or `apt install docker.io` for the distribution build)."),
    ToolDef("aws", "AWS CLI", "SRTM download (s3)", ("aws",), required=False,
            install_help="`apt install awscli`, or `pip install awscli`."),
    ToolDef("java", "Java (JRE 21+)", "Tiles stage (planetiler, Phase 7)", ("java",), version_args=("-version",),
            min_version=(21,), required=False, apt="openjdk-21-jre-headless"),
    ToolDef("valhalla", "Valhalla (compiled)", "Valhalla stage", kind="built", required=False,
            install_help="Needs git, cmake, make and g++. Press Build: the configured version (Settings → Tools → Valhalla version) "
                         "is cloned and compiled into the data root (tools/valhalla), which takes a while."),
    ToolDef("planetiler", "planetiler.jar", "Tiles stage (Phase 7)", kind="file", required=False,
            default_file="tools/planetiler.jar",
            install_help="Not in apt: download planetiler.jar from https://github.com/onthegomap/planetiler/releases "
                         "to the default location (data root, tools/planetiler.jar) or set its path here."),
)
BY_KEY = {t.key: t for t in TOOLS}
