# `data-manager`: Redesign of the SwayRider Data Pipeline

## Context

SwayRider's map/routing data pipeline (`data-pipeline`) and its deployment scripts (`infra/dev/scripts`, `infra/dev-mini/scripts`) grew as five independent CLI scripts plus a pile of bash, without shared orchestration, structured state, or a real deployment/rollback story. Git history shows this is being actively firefought (manifest/completion bugs, tiles OOM issues, sudo/permission issues, silent config typos disabling steps, exit codes that are always 0). Region configuration today is hand-typed YAML with heavy duplication — e.g. Germany's full `overlap.openaddresses` list (26 per-state files) is copy-pasted verbatim into `benelux`, `france`, and `germany`'s configs in `config-mini.yml` — with no validation and no visual way to see what a region actually covers.

The user wants a ground-up redesign, built as a new Flask app in a new `data-manager` folder, that owns the whole lifecycle: configuration → download → build → asset tracking → assembly → publish → deploy, with real progress tracking and error monitoring throughout. Two decisions from the first planning pass have since been revised by the user: **no import of legacy pipeline output** (clean slate — `data-manager` builds everything itself from zero recorded state) and **configuration must be authored through a UI**, specifically a map on which the operator selects which countries belong to which region, rather than hand-edited YAML.

This plan is planning/architecture only — **no code is written in this phase**.

### Decisions locked in with the user
1. **Scope**: full redesign, including rewriting the domain/processing logic itself (OSM extraction, border-crossing detection, Valhalla build, Pelias import, tiles generation), not just an orchestration layer bolted onto the existing `pipeline/*.py`.
2. **Deployment**: `data-manager` fully absorbs deployment — extraction into service data dirs, triggering restarts itself, plus an active-release pointer and real rollback (all currently missing).
3. **Execution model**: long-running stages run via a background job queue, not in-Flask-process.
4. **State storage**: SQLite is the source of truth for configs, downloads, build runs, assets, assembly, publish, and deployment records.
5. **Deploy topology**: colocated-first, architected as a pluggable deploy-target so a remote/SSH target can be added later without a redesign.
6. **Migration**: clean replacement, **clean slate** — no importing or registering old `data-pipeline` output as `data-manager` assets. `data-manager` starts empty and produces every asset it ever tracks itself. Old scripts are retired stage-by-stage as `data-manager` gains equivalent, real coverage — there is no shortcut where old output is grandfathered in.
7. **Configuration authoring**: no hand-edited YAML as the primary interface. `data-manager` provides a configuration-builder UI, centered on a map where the operator selects countries and assigns them to a region (core vs. overlap), instead of typing country/WOF/OpenAddresses/SRTM lists by hand.

---

## Current State (what's being replaced)

- **5 independent CLI pipelines** in `data-pipeline/pipeline/`: `osm_pipeline.py`, `border_pipeline.py` + `border_crossing.py`, `valhalla_pipeline.py`, `pelias_pipeline.py`, `tiles_pipeline.py` + `tiles.py` (3112 lines — the largest, riskiest stage). Dependency graph: `osm → {border, valhalla → pelias}`, `tiles` independent.
- **Config**: `config/config-dev.yml` (8 regions) and `config/config-mini.yml` (3 regions) are tracked templates; the live `config/config.yml` is gitignored and manually derived. Each region (`pipeline/config/region.py`) is defined by hand as: `core`/`overlap` blocks, each with an `osm` list (Geofabrik paths like `europe/belgium`), a `wof` list (WhosOnFirst/ISO-ish country codes), and an `openaddresses` list (per-country **and often per-subdivision** file paths, e.g. Germany needs 26 separate state/region CSV paths) — plus a `srtm` block of per-country lat/lon bounding boxes, and optional `gtfs_feeds`. `border-regions` is just a list of `[regionA, regionB]` name pairs, resolved into `.poly` intersection files by `generate_polygons.py` (Natural Earth admin-0 country polygons, matched by lowercased WOF/ISO code, unioned per region, buffered 100km for overlap zones / 10km for border-intersection zones). **Because the same country (e.g. Germany) appears as an "overlap" country in multiple regions, its entire OpenAddresses file list is duplicated verbatim across every region that borders it** — this is the concrete evidence that per-country reference data should be normalized once, not repeated per-region.
- **State/progress**: one YAML manifest per pipeline (`pipeline/manifest/manifest.py`), used purely for resume-on-crash skip logic. No structured logging, exit codes always 0.
- **Publish** (`pipeline/publish.py`): moves archives into `{geodata_dir}/{tag}/` with no cross-check that all 5 pipelines share one tag/region set.
- **Deploy** (`infra/dev/scripts/deploy-*.sh`, `infra/dev-mini/scripts/deploy.sh`): manual bash extraction + quirky per-type fixups (e.g. renames `tiles.tar`→`valhalla_tiles.tar`), **prints** restart instructions rather than executing them, no active-release pointer, no rollback, no retention.

---

## Architecture

### System shape

```
                 ┌────────────────────────────┐
   operator ───► │   Flask app (dashboard+API) │ ◄── reads/writes ──► SQLite (WAL)
                 └───────────┬────────────────┘                         ▲
                             │ enqueue                                  │ writes progress/
                             ▼                                          │ status/assets
                 ┌────────────────────────────┐                         │
                 │  Redis (RQ broker/queue +   │                         │
                 │  pub/sub for progress)      │                         │
                 └───────────┬────────────────┘                         │
                             │ dequeue                                  │
                             ▼                                          │
                 ┌────────────────────────────┐   subprocess (osmium,   │
                 │  RQ worker process(es)      │───cmake/make/npm,──────┘
                 │  running stage runners      │   tippecanoe, docker)
                 └───────────┬────────────────┘
                             ▼
       downloads/ , library/ (content-addressed assets), work/<run_id>/ (scratch),
       releases/<tag>/, deploy-state/<env>/current (active-release symlink)
```

Flask never runs domain logic itself — it's a read/write layer over SQLite plus a job-submission client. **Job queue: RQ** (coarse-grained, single-operator, single-broker — Celery's extra power isn't needed; `infra/dev/layer-00` already runs Redis). **Progress/errors: Server-Sent Events**, not WebSockets (one-directional, no extra infra, degrades to polling). **UI: server-rendered dashboard (Flask + Jinja + htmx/SSE)**, API-first underneath — a single operator needs tables, statuses, progress bars, action buttons, and now a map, not a SPA build toolchain layered on a domain that already carries osmium/cmake/npm/tippecanoe.

**Filesystem layout**: `state/db.sqlite3` (WAL); `downloads/<source_key>/<version_or_hash>/…`; `library/<asset_type>/<scope>/<hash_prefix>/…` (content-addressed); `work/<run_id>/…` (per-run scratch, deleted per-run); `releases/<tag>/manifest.json`; `deploy-state/<environment>/current` (active-release symlink).

### Configuration model and the country catalog

Configuration is split into two layers, confirmed by diffing `config-dev.yml`/`config-mini.yml`:
- **Base** — tool-build specs (valhalla/pelias/tippecanoe/osgeo), download URL templates, global tunables, osmium export-config references. Near-identical across today's templates; shared across profiles.
- **Profile** (e.g. `dev`, `dev-mini`, or any new one the operator creates) — a named set of **regions**, **border-regions**, `tile_regions`, `region_size`/`tile_size`, and filesystem paths.

Sitting underneath both, as genuinely new reference data, is a **country catalog** — the piece that makes the map UI possible and that eliminates the OpenAddresses-list duplication described above:

- `country(iso2 PK, name, ne_geometry_ref, bbox_json, geofabrik_path, wof_code, srtm_bbox_json, openaddresses_files_json)` — one row per country, seeded once from Natural Earth admin-0 polygons (already downloaded today, per `generate_polygons.py`'s `load_ne_countries`) for geometry/bbox, plus a curated mapping for `geofabrik_path` (Geofabrik's per-country URL path doesn't derive cleanly from ISO code alone, e.g. `europe/belgium`), `wof_code` (matches today's lowercase ISO convention, needs occasional exceptions), `srtm_bbox_json` (auto-computable from the country's Natural Earth geometry bounds, rounded outward to whole degrees — replacing today's hand-typed per-country boxes), and `openaddresses_files_json` (the genuinely bespoke part — some countries have one `countrywide.csv`, others like Germany/Belgium need multiple per-subdivision files; this list has to be curated once from the OpenAddresses source index, not auto-derived from geometry).
- **Implementation note:** `config_document` is currently realised as a simple mutable `config_profile(id, name, description, timestamps)` table (migration 0002, managed in the Configure header). Immutable revisions / `config_resolved` are deferred until a stage consumes a resolved config; `region_def` will then FK to `config_profile.id` (`ON DELETE CASCADE`).
- **Address sources:** per-country address sources are stored in `country_address_source(country_iso→country, source_key, enabled, config_json)` (migration 0004; unique per country+source), backed by a code-side registry in `datamanager/address_sources/`. Registered: `openaddresses` (config `{"files": [...]}`, stored without extension) and `overture` (Overture Maps addresses, enabled by default for the 41 countries in its address theme). OpenStreetMap `addr:*` needs no row: it comes from the Geofabrik extract for every country with a Geofabrik path. The Pelias stage (Phase 6) consumes these as the old pipeline did (OpenAddresses importer, Overture via `overturemaps download --bbox` → csv-importer, OSM importer). Known gap: Bavaria has no OpenAddresses or Overture coverage (official Hauskoordinaten have been open data since 2023; licence/download unconfirmed) — country-specific sources are future work.
- `region_def(id, config_document_id→ (profile), name, gtfs_feeds_json)`.
- `region_country(id, region_def_id→, country_iso→country, role['core'|'overlap'], selected_openaddresses_files json)` — *Implementation note:* realised as `region` + `region_country` (migration 0006); `region_country` also carries a denormalised `config_profile_id` with a unique `(config_profile_id, country_iso)` so a country is in at most one region per configuration. Only role `core` is stored here. **Overlap is stored in `region_overlap(region_id, country_iso, mode auto|include|exclude)`** (migrations 0007/0008): `auto` rows are the countries within 100 km (`OVERLAP_BUFFER_M`, true distance) of the region's core, **recomputed by `services.regions.evaluate_overlap` whenever the core changes** (add/remove country; `flask evaluate-overlap [--all]`; regions never evaluated are evaluated on first read, tracked by `region.overlap_evaluated_at`); `include` = forced in by the user, `exclude` = detected but dropped. Re-evaluation keeps overrides unless they became meaningless (stale exclude, country became core). Effective overlap = auto + include. Border regions stay derived: region pairs whose cores' 10 km buffers (`BORDER_BUFFER_M`) intersect (`services/overlap.py`). Overlap countries without a Geofabrik path are flagged "needs configuring". Buffers are **true distances**: `overlap.buffer_m` projects to a local azimuthal equidistant CRS centred on the geometry, so 100 km is 100 km at any latitude. This intentionally differs from the legacy `.poly` output (EPSG:3857, where 100 km ≈ 64 km at 50°N); on dev-mini it only adds Sweden to Germany's overlap. Only configured (curated) countries can be assigned; regions have an editable `color` (auto-picked from a palette). — *`selected_openaddresses_files`* is realised inverted as `region_openaddresses_exclusion(region_id, country_iso, file)` (migration 0009): every file the catalog knows for a core/overlap country is included by default, and the **Resolved** tab lets you deselect files per region (stale exclusions are ignored, files added to the catalog later default to included). `services/resolve.py` derives the per-region resolved config from all of the above: `core`/`overlap` × (Geofabrik paths, WOF codes, OpenAddresses files), `srtm` boxes for core **and** overlap countries (keyed by the last Geofabrik path segment, as in `config-mini.yml`), warnings (unconfigured overlap country, no core, no address source) and a per-region sha256 hash of the build-relevant content (the future `config_resolved.resolved_hash`). It can be exported in the legacy `config-mini.yml` shape (OpenAddresses entries get `.csv` back) or as JSON. **Carving** (`services/carve.py`, table `country_carve`, migration 0011): per configuration a country can be reduced to a drawn keep-polygon (e.g. metropolitan France without French Guiana); overlap detection, border pairs, the buffer and tile coverage use the clipped geometry. The catalog's `srtm_bbox_json` is *not* carve-aware (still hand-curated). **Stage requirement (nothing consumes it yet):** the resolved config carries each carved country's keep-polygon (`carve`, part of the region hash); the OSM stage must clip the extracted PBF to it (osmium `extract -p`), and later stages (Pelias/OpenAddresses filtering, SRTM tile selection, tiles) must honour it too. **Tiles** (`services/tiles.py`, tables `tile_settings`, `tile_override`, `tile_source_override`, migration 0010): `region_size` is derived as the bounding box of every region's core + overlap land clipped to the 100 km zone around the core (so Spain only counts near France), snapped outward to `tile_size` (default 10°) — for benelux/france/germany this gives exactly the legacy 40–60 N / -10–20 E; tiles are the grid cells that actually intersect that land (`N50_E000` naming), `tile_regions` are the first Geofabrik path segments in use (`europe`). All derived on read (no re-evaluation step); the user can set a manual extent, toggle single tiles and add/drop download regions. They appear in the Resolved tab, JSON and legacy YAML (`region_size`, `tile_size`, `tile_regions`). **Global settings** (`services/settings.py`, table `global_setting`, migration 0013, Settings section): a code registry of keys with defaults and validation (`public.tiles_url/glyphs_url/sprite_url` used by the exported map styles, `download.osm/srtm/natural_earth/land_polygons`, `run.max_workers`, `run.java_xmx`, `tool.valhalla_tag`, `tool.elasticsearch_version`); the DB stores only overrides and resetting deletes the row. Stages will read these when they land (nothing but the style export consumes them yet). **Required tools** (`datamanager/tools.py` registry, `services/tools.py` detection, Settings page): osmium, GDAL, git/cmake/make/g++, node/npm, rsync/ssh (required) and curl, unzip, docker, aws, the `pmtiles` CLI (optional/later; replaced java + planetiler.jar, obsolete since the Protomaps decision, on 2026-10-02) are detected on PATH (or a custom path saved as `toolpath.<key>`), the version is parsed and compared with a minimum, results are cached per process and re-detected on demand (per-tool and all buttons), Debian install commands are shown, and the sidebar shows a warning while a required tool is missing. **Stage requirement:** every stage must call `services.tools.detect(..., force=True)` for the tools it needs before starting and fail with a clear error when one is not ok (not enforced yet). Valhalla and the Pelias repos are built into the tools dir by their stages and are not detected. **GTFS feeds** (Configure → Transit tab, table `region_gtfs_feed`, migration 0014): per region a list of feed URLs (`gtfs_feeds_json` realised as rows with an optional label, unique per region, http/https only, no reachability check). They appear in the Resolved tab, JSON and legacy YAML (`gtfs_feeds`, only when non-empty) and in the region hash (sorted URLs, only when set, so regions without feeds keep their hash). **Stage requirement (nothing consumes it yet):** the Pelias download stage downloads a region's feeds to `data/gtfs/<region>/feed_NNN.zip` in list order. This completes Phase 1.
- `border_region_def(id, config_document_id→, region_a_id→region_def, region_b_id→region_def)`.

Resolving a profile into the exact shape `Region`/`BorderRegion` need today (core/overlap `osm`/`wof`/`openaddresses` lists, an `srtm` block) becomes a pure, testable derivation over `region_country` joined to `country` — no more hand-typing, no more duplicated lists, and every resolved config is hash-addressable (`config_resolved.resolved_hash`) and traceable to the exact region/country selections that produced it. `config_document`/`config_resolved` from the original design are retained, but **structured UI edits are the primary authoring surface**, not raw YAML; a generated YAML/JSON export remains available for audit/diffing/download, not for editing.

### Configuration Builder UI — map-based region editor

This is the feature the user specifically asked for, and it's the first thing built on top of Phase 0's foundations (see plan below), since without it there is no way to produce a real profile at all under the clean-slate decision. At the architecture level: a client-side map (country-boundary layer generated from the same Natural Earth admin-0 source already used for `.poly` generation) lets the operator assign countries to a region's core/overlap sets, manage border-region pairs, and define the tiles stage's independent bbox extent; saving produces a new immutable `config_document` (profile) revision and a resolved `config_resolved` row for stage runners. The exact interaction design (click behavior, layout, base-settings forms, map library choice) is intentionally left open for when this phase is actually built, rather than specified now.

### Stage → runner dependency graph (unchanged, confirmed against actual code)

| Stage | Consumes (assets) | Consumes (downloads) | Produces | Depends on |
|---|---|---|---|---|
| osm | — | Geofabrik PBFs, SRTM, Natural Earth | osm-pbf, osm-core-pbf, region/overlap/border polygons | — |
| border | osm-pbf, osm-core-pbf | — | border-geojson, border-crossings-csv | osm |
| valhalla | osm-pbf | — | valhalla-tiles, admin.sqlite, tz.sqlite, polylines | osm |
| pelias | osm-pbf, valhalla polylines | WOF, geonames, OpenAddresses | pelias-index-snapshot, pelias-config, pelias-wof | osm, valhalla |
| tiles | Protomaps daily planet build (`pmtiles extract` of the configured extent) | — | `tiles.pmtiles` (Protomaps schema, Z0–15) + light/dark `style.json` — *target; legacy: mbtiles L0/L1/L2 per tile* | — (genuinely independent) |

### Database schema (SQLAlchemy + Alembic)

In addition to the configuration tables above:

**Downloads**: `download_source(id, key, kind['http'|'s3'], url_template, version_strategy['etag'|'content-hash'|'static-pin'|'latest-no-signal'])`; `download_record(id, download_source_id→, resolved_url, version_label, content_hash, size_bytes, fetched_at, local_path, status, superseded_by_id→, retention_class)`.

**Builds**: `build_stage_def(id, key, depends_on json)` (DB mirror only — the Python stage-runner registry is the authoritative DAG); `build_run(id, stage_key→, config_resolved_id→, tag, scope, status, started_at, finished_at, rq_job_id, triggered_by, retry_of_run_id→)`; `build_step(id, build_run_id→, name, sequence_index, status, started_at, finished_at, progress_current, progress_total, progress_message, error_type, error_message, error_traceback_ref)` — verbose subprocess output lives as JSON-lines files under `work/<run_id>/logs/`, not in SQLite.

**Assets**: `asset(id, asset_type, scope, produced_by_run_id→build_run, path, content_hash, size_bytes, created_at, is_current, superseded_by_asset_id→asset, source_download_ids json)`. Each runner declares `produces`/`consumes`/`consumes_downloads`; submitting a run resolves this DAG structurally, replacing today's comment-only ordering constraints.

**Assembly / Publish / Deploy**: `assembly(id, tag, config_resolved_id→, requested_at, status, validated_at)`; `assembly_item(id, assembly_id→, asset_id→, role)` — status becomes `complete` only once every required (stage, scope) combination has an item, fixing `publish.py`'s cross-manifest-tag flaw; `publish_record(id, assembly_id→, tag, geodata_store_path, contents_manifest_json, published_at, published_by)`; `deployment_target(id, key, kind['local'|'ssh'], connection_config_json, service_map_json)`; `deployment_record(id, deployment_target_id→, publish_record_id→, tag, status, deployed_at, deployed_by, rolled_back_from_deployment_id→)`.

Traceability is one join: `deployment_record → publish_record → assembly → assembly_item(s) → asset(s) → build_run(s) → config_resolved → region_def/region_country/country` and `download_record(s)`.

### Download manager: versioning & retention (unchanged)

Three tiers: real version signal (ETag/Last-Modified) where sources support it; hash-and-dedup re-request for Geofabrik's `-latest` naming (fixing today's presence-only skip logic, which never re-checks upstream at all); fetch-once-and-pin for static sources (SRTM, Natural Earth). Retention is reference-counted via `asset.source_download_ids`, with a dry-run-by-default GC job — replacing today's binary keep-everything vs. whole-tree `rmtree`.

### Deploy-target abstraction, active-release pointer, rollback (unchanged)

`DeployTarget` interface (`resolve_service_map`, `put`, `run_command`, `restart_services`, `symlink_current`/`read_current`); `LocalDeployTarget` first, `SSHDeployTarget` addable later without touching orchestration logic. Active-release pointer is a `deploy-state/<env>/current` symlink flipped only by the deploy action. Rollback is not a special code path — it re-runs the same deploy sequence against an older `publish_record`, guaranteeing rollback always exercises the same tested path as forward deploys. **Because there is no legacy import, the first real deploy exercise happens once the OSM stage runner (Phase 2 below) produces real assets** — conveniently the simplest case in the existing service table (OSM → `{GEODATA_PATH}/osm/`, no service restart needed), making it a good low-risk first target before border/valhalla/pelias (each requiring a restart) and pelias (requiring ES snapshot restore + alias switching).

### Flask surface

`/api/configs`, `/api/countries`, `/api/regions` (+ border-regions), `/api/downloads`, `/api/runs` (+ `/runs/<id>/events` SSE, `/runs/<id>/logs`), `/api/assets`, `/api/assembly`, `/api/publish`, `/api/deploy` (+ rollback), `/api/system`. Errors are classified into a structured taxonomy (`DownloadError`, `ToolBuildError`, `SubprocessError`, `ValidationError`, `DependencyMissingError`, …), persisted to `build_step`/`build_run`, and re-raised so RQ's failed-job registry also catches crash-only failures. A stall-watchdog flags `running` runs with no progress past a timeout.

---

## Modular Development Plan

Sequencing is unchanged in spirit (OSM first, tiles last, deploy absorption pulled forward), but revised for clean-slate: there is no imported-legacy-data shortcut, so the configuration builder must exist before any real profile can be defined, and deploy/rollback is proven against the OSM stage's real output rather than against imported tarballs.

- **Phase 0 — Foundations**: Flask skeleton; SQLite + SQLAlchemy + Alembic; Redis + RQ wiring; structured logging + error taxonomy; stage-runner contract interfaces exercised against a no-op stage; **country catalog seed pipeline** (ingest Natural Earth admin-0 for geometry/bbox, curate Geofabrik-path/WOF/OpenAddresses-file reference data — this curation is real, one-time manual work, called out as a risk below). No operator-visible pipeline change yet.

- **Phase 1 — Configuration Builder UI**: the map-based region editor described above — country selection, core/overlap assignment, border-region pairing, tile-extent drawing, base-layer settings forms; `config_document`/`config_resolved` resolution and validation. Operator can now fully define a `dev`-equivalent and `dev-mini`-equivalent profile (or any new one) through the UI, with zero hand-written YAML. No builds run yet.

- **Phase 2 — Download manager + OSM stage runner** (*Implementation note, step 1 done: run infrastructure + OSM downloads.* Tables `build_run`, `build_step`, `download_record` (migration 0015). Every stage is a separately runnable run that ends `awaiting_review`; approving is the gate and only approved runs feed later stages. Downloads are versioned by UTC fetch time, several versions coexist, stages use the newest approved version unless a version is pinned or the run overrides it (`build_run.input_versions_json`); cleanup keeps the newest N plus pinned/in-use versions and is dry-run first. `download_source` is not a table (sources are derived from the resolved config). *Step 2 done: polygons.* Table `asset` (migration 0016) and the `polygons` stage: per region `<region>-core`, `<region>-overlap` (100 km, true distance) and per bordering pair `<a>-<b>-border` (intersection of the 10 km buffers), plus `carve-<iso>` keep-polygons, written as `.poly` (osmium `extract -p` verified) and `.geojson` under `library/assets/polygons/<run_id>/`; the run report lists area, vertices, bbox and validity per polygon and the run page shows them on a map. Assets follow the download rule: usable by later stages only once their run is approved. Cleanup of download versions still gains a "referenced by an asset" condition once the extract consumes them. *Step 3 done: `osm-extract`* (`stages/osm_extract.py`, `services/osmium.py`): per region `<region>-core.osm.pbf` (core countries merged, carved ones clipped to `carve-<iso>`) and `<region>.osm.pbf` (core + overlap countries clipped to the approved `<region>-overlap.poly`), merged with osmium sort + time-filter so border objects keep their newest version; inputs are the approved (or pinned/overridden) download versions and approved polygon assets, recorded in `asset.source_download_ids`; `params.regions` builds single regions; the report shows counts, bbox, inputs and warnings (stale input, unsorted, duplicate versions, data outside the overlap polygon). Downloads a live asset was built from are protected from cleanup; rejected assets lose their files. *Step 4 done: OSM source = planet* (migration 0017 adds `download_record.data_timestamp`). Per-country Geofabrik downloads were too slow (the server throttles a long-lived connection to ~100 kB/s while fresh connections run at ~10 MB/s), so the default route is: `download-planet` (source `planet:osm`, URL in setting `download.planet`, default the FAU mirror; ETag/Last-Modified unchanged means no download; md5 sidecar verified; at most `download.planet_keep` = 2 versions kept, pruned after fetch and after approval; the file may also be registered from disk with `local_file`, or the newest version reused without any upstream check with `use_existing`, for testing) then `extract-countries` (cuts every core/overlap country of the configuration out of the approved planet in one `osmium extract -c` pass using Geofabrik's published `<path>.poly` files, fetched as `poly:<path>` versions; a country already extracted from the same planet version and polygon hash is not extracted again; osmium keeps ID sets per region (~2.6 GB each on the planet, 47.7 GB for 18 regions in one run), so countries are cut in batches of `run.extract_memory_gb` / 3 (default 16 GB: 5 per run, each batch re-reads the planet), osmium is killed when available memory drops below `run.extract_min_free_gb` (default 4 GB), batches that finished stay registered so a re-run only cuts what is missing, and the report shows batches and osmium's peak memory; results are configuration-independent `country-pbf` assets, newest 2 approved per country kept). `osm-extract` takes its inputs from these assets when `osm.source = planet` (default) and from Geofabrik download versions when `geofabrik`. Large downloads (`services/downloads.py`) use short 128 MB Range segments over fresh connections (`download.connections` in parallel), drop connections slower than 1 MB/s for 20 s, retry per segment and check free disk space. *Step 5 done: `download-tiles`* (source `tiles:planet`; newest build from the Protomaps build list, segmented download, PMTiles v3 header and metadata read by `services/pmtiles.py`, warnings for old build / wrong layers / zoom < 15, newest `download.tiles_keep` kept; the planet-wide file is the input of the later extent extract). *Step 6 done: styles ported to the Protomaps schema* (`services/map_styles.py`, `datamanager/styles/protomaps-*.json`; the Style tab previews over the approved `tiles:planet` file, served with Range from `/configure/style/tiles.pmtiles`). *Step 7 done: `styles`* (`stages/styles.py`: per configuration `style-light.json` / `style-dark.json` assets from the Style tab choices, label zooms and Settings → Public URLs; "outdated" when the built styles' fingerprint changes). *Step 8 done: `download-srtm`* (`stages/download_srtm.py`, `services/srtm.py`: the 1° Skadi tiles of the regions' SRTM boxes, one versioned `srtm:N50E004` download each, unchanged upstream = no new version, open-sea tiles (404) only counted, unpacked once per content hash into `library/srtm-cache/` and hard-linked as `library/srtm/<run_id>/N50/N50E004.hgt` for Valhalla; newest `download.srtm_keep` versions kept after approval). Natural Earth is downloaded by the country seeder, so Phase 2 is complete.): `download_source`/`download_record` + the three-tier versioning/retention design; OSM runner (region merge/extract via osmium, polygon generation) consuming a real `config_resolved` from Phase 1, producing real `osm-pbf`/`osm-core-pbf`/polygon assets. **Retires**: `prepare-source-data` / `OsmPipeline`.

- **Phase 3 — Assembly, Publish, Deploy (local), active-release pointer, rollback** (*decision 2026-10-01: deferred until after the stage runners (Phases 4–6); the deploy target is still undecided. It was pulled forward only to prove the deploy/rollback loop on the simplest asset.*): originally planned to be built and proven against Phase 2's real OSM-only assets (no restart required — the simplest real case). `assembly`/`publish_record`, `deployment_target` (local), `deployment_record`, active-release pointer, and rollback all get their first live exercise here, for real, before any other stage exists. **Retires**: `infra/dev/scripts/deploy-osm.sh` and (once dependency-order requires it) the rest of `deploy-*.sh`/`deploy-all.sh`, `infra/dev-mini/scripts/deploy.sh`, `data-pipeline/publish`.

- **Phase 4 — Border stage runner** (*implemented 2026-10-01: `stages/border.py` + `services/borders.py`. Per region `<region>-core.geojson` / `<region>-extended.geojson` (`region-outline` assets: admin_level=2 boundary multipolygons of the approved region PBFs, unioned) and per bordering pair `<a>-<b>.csv` (`border-crossings`, same columns as the legacy output; `<a>` is the first region in slug order, its full PBF clipped to the approved border polygon, crossings measured against its core outline). The legacy pyosmium handler is replaced by `osmium tags-filter` + `osmium export` (linestrings) and shapely, so there is no pyosmium dependency; the STRtree, before/after-point test and `oneway` handling are ported unchanged. Pairs/regions whose inputs (asset hashes) are unchanged since the approved asset are skipped. Not yet verified against a legacy run on real data; no review map yet, only tables.*): consumes OSM assets through the dependency resolver (first proof of structural ordering); STRtree/oneway-aware border-crossing handler ported with test coverage; deploy extended to `regionservice` restart. **Retires**: `build-border-data` / `BorderDataPipeline`.

- **Phase 5 — Valhalla stage runner** (*implemented 2026-10-02: the compile is not a pipeline step but a tool under Settings → Tools (`services/valhalla_build.py`, `tools/valhalla/state.json`, Build/Rebuild button, RQ job `build_tool`; detection only reads the record and checks the binaries). `stages/valhalla.py` + `services/valhalla_data.py` build per region `tiles.tar`, `admin.sqlite`, `tz_world.sqlite` and `polylines.0sv.gz` (`valhalla-*` assets) from the approved full PBF and the newest approved `download-srtm` run; fingerprint = PBF hash + Valhalla version/flags + hashes of the region's SRTM tiles. The timezone database is built once per run. Not yet run against a real Valhalla build.*): self-compiled-tool pattern retained; tool builds cached/versioned by repo+tag+build-flags hash; deploy extended to per-region Valhalla restart (including the `tiles.tar`→`valhalla_tiles.tar` rename, now a declarative deploy step). **Retires**: `build-valhalla-data` / `ValhallaDataPipeline`.

- **Phase 6 — Pelias stage runner**: Docker ES + npm-cloned tool repos; in-place third-party patches become explicit, versioned, tracked steps; first multi-parent dependency test (osm + valhalla); deploy extended to ES snapshot restore + alias switching. **Retires**: `build-pelias-data` / `PeliasDataPipeline`, `scripts/import-pelias-dev.sh`.

- **Phase 7 — Tiles stage runner** (last, highest risk): the ~11-step post-processing pipeline decomposed into an ordered `Step` contract; per-tile failures become retryable units; `--gen-tile` becomes a dashboard "retry this asset" action. **Retires**: `build-tiles` / `TilesPipeline`. **Superseded in approach by "Tiles stage: Protomaps PMTiles extract" below** (a download + `pmtiles extract` instead of the decomposed pipeline; per-tile retry no longer applies).

- **Phase 8 — Full cutover and cleanup**: `infra` trimmed to Compose topology only; old `manifest-*.yml` usage fully removed; SSH deploy target added if/when a second host appears; scheduled checks for pinned near-static downloads; dashboard polish (lineage graph, deployment timeline).

**Cross-cutting concerns established in Phase 0, not deferred**: structured logging/error taxonomy, config schema validation, DB migration discipline.

---

## Phase 0 — Detailed Plan (first step)

Grounded in confirmed facts: `data-pipeline` uses pip + `requirements.txt` (pinned), targets Python 3.11+, has no `.env` (YAML-only config), and its CLI convention is executable root scripts with `--config <path>`. `infra/dev/layer-00` already runs a shared Redis (`sw-dev-redis`) used by other services, reachable from host processes at `redis://localhost:36379` — but **`data-manager` gets its own dedicated Redis, not that shared one**, and its own first-class place in `infra` (unlike `data-pipeline`, which is a bare host script with no Compose layer of its own). Also newly confirmed: `config-dev.yml`'s 8 regions reference **49 distinct ISO2 country codes** in total (`config-mini.yml`'s 6 are a subset) — this is the real scope of Phase 0's country-catalog acceptance bar, not just Benelux/France/Germany.

### Infra placement — `infra/data-manager/`

- A new top-level folder in the `infra` repo, parallel to `infra/dev`/`infra/dev-mini`, containing its own `compose.yaml`. This is the "separate infra" decision: `data-manager` is a long-lived service, so it earns the same first-class Compose treatment as `layer-00`/`layer-10`/etc., rather than being an unmanaged host script.
- **Own dedicated Redis** (`sw-datamanager-redis`, `redis:7-alpine`, ephemeral/no persistence — it's only an RQ broker+queue, same style as `sw-dev-redis`), on a **different host-mapped port** than layer-00's `36379` (e.g. `36389`, exact number TBD at implementation time to avoid collision with whatever else is already mapped) so it doesn't collide with the shared one.
- **Both containerized and host-run access must work against the same Redis**, since local debugging means running Flask/the RQ worker directly on the host (not in a container) while pointing at this Redis:
  - Containers in the `infra/data-manager` compose stack reach it via the internal service hostname (e.g. `redis`).
  - A host-run `flask run` / `python run_worker.py` (the normal Phase 0 development loop) reaches the *same* Redis via the mapped port: `redis://localhost:36389/0`.
- The compose file also defines (or reserves) the `data-manager` Flask app and RQ worker as services, for eventually running the whole thing as an always-on stack — but Phase 0's actual development and acceptance testing happens by running Flask/the worker on the host against the compose-provided Redis, which is why the dual-reachability requirement matters now, not just later.
- `AppConfig.REDIS_URL` defaults to the host-reachable form (`redis://localhost:36389/0`) for local development; a container-run overrides it via env var to the internal hostname. This is the only difference between the two run modes.

### Repo layout

```
data-manager/
├── requirements.txt          # pinned, pip — mirrors data-pipeline
├── .env.example               # checked in; .env itself gitignored (local overrides only)
├── alembic.ini
├── wsgi.py                    # create_app() entrypoint
├── run_worker.py              # RQ worker entrypoint
├── migrations/                # Alembic, versions/0001_create_country.py
├── datamanager/
│   ├── config.py              # AppConfig — env-driven bootstrap config (DB path, REDIS_URL, DATA_ROOT)
│   ├── db.py                  # engine/session factory, WAL pragma hook (shared by Flask, worker, Alembic)
│   ├── errors.py               # exception taxonomy
│   ├── logging_setup.py        # structured logging, shared by Flask + worker
│   ├── models/
│   │   ├── base.py             # declarative Base
│   │   └── country.py          # Country — the only real table in Phase 0
│   ├── stages/
│   │   ├── contract.py         # StageRunner ABC, StageIO, StageRunContext, StageResult
│   │   ├── registry.py         # StageRegistry: register / resolve_order (topological sort)
│   │   └── noop.py             # NoOpStage synthetic stage
│   ├── jobs/
│   │   ├── queue.py            # Redis connection + RQ Queue from AppConfig.REDIS_URL
│   │   └── tasks.py            # job functions (ping, run-noop-stage)
│   ├── countries/
│   │   ├── natural_earth.py    # ensure download + load_ne_countries() (ported from generate_polygons.py)
│   │   ├── curated/countries.yml  # hand-curated geofabrik_path / wof_code / openaddresses_files
│   │   └── seed.py             # seed_countries(session): merge NE + curated → upsert Country rows
│   ├── web/health.py           # /health — only route in Phase 0
│   └── cli.py                  # `flask seed-countries`
├── data/                       # gitignored runtime root: state/, downloads/, library/, work/, releases/
└── tests/
```

**Bootstrap config**: `datamanager/config.py` reads env vars (`REDIS_URL`, `DATABASE_PATH`, `DATA_ROOT`, …) with local-dev defaults (`REDIS_URL` defaulting to `redis://localhost:36389/0` — the host-mapped port of `infra/data-manager`'s dedicated Redis), optionally loaded from a gitignored `.env` via `python-dotenv`. This is a different concern from `data-pipeline`'s "no YAML config" convention — it's "where's my DB/broker," not domain configuration (which stays DB-driven per the Configuration model above, starting Phase 1). Plain SQLAlchemy (not Flask-SQLAlchemy), since the RQ worker needs a DB session with no Flask app/request context; Alembic's `env.py` imports the same `db.py`/`config.py` so migrations and the running app can never target different SQLite files.

### Build order

1. **Country catalog curation** (research track — start immediately, runs in parallel with everything below, gates only the *final* seed acceptance, not the code). Enumerate the real 49-code target set from `config-dev.yml` and hand/semi-scripted-curate `geofabrik_path`, `wof_code` exceptions, and `openaddresses_files` into `countries/curated/countries.yml`. This is open risk #1 below made concrete — flagged as schedule risk, not technical risk.
2. **Flask skeleton** — package scaffold, `create_app()`, `/health`, `.env.example`. No DB/Redis dependency.
3. **Structured logging + error taxonomy** — needed by every later step.
4. **SQLAlchemy + Alembic + `Country` model** — `db.py` WAL hook, `models/country.py`, revision `0001_create_country`.
5. **Stage-runner contract + no-op stage + DAG resolver** — pure Python, no DB; can be built in parallel with step 4.
6. **RQ wiring** — bring up the `infra/data-manager` compose stack's dedicated Redis, prove connectivity from the host at `redis://localhost:36389/0` with a trivial `ping` job first, then wire `NoOpStage.run()` as a real job body.
7. **Country catalog seed script** — can be written/tested against a small stub of `countries.yml` while step 1's curation is still in progress; run for real once curation lands.
8. **Phase 0 acceptance pass** (checklist below).

### Stage-runner contract

```
StageIO(asset_type: str)

StageRunner(ABC):
    key: str
    produces: tuple[StageIO, ...] = ()
    consumes: tuple[StageIO, ...] = ()
    consumes_downloads: tuple[str, ...] = ()   # unused until Phase 2
    def run(self, context: StageRunContext) -> StageResult: ...

StageRegistry:
    register(stage_cls)
    resolve_order(requested_keys) -> list[str]   # topological sort; raises CyclicDependencyError /
                                                   # UnresolvedDependencyError from errors.py
```

`NoOpStage` (`key="noop"`, `produces=("noop-output",)`) reports progress and writes a marker file under `work/<run_id>/`, proving that filesystem convention even in Phase 0. Tests: producer→consumer chain resolves in order; an independent stage schedules alongside a chain; unmet `consumes` raises `UnresolvedDependencyError`; a cycle raises `CyclicDependencyError`; `NoOpStage` run directly vs. submitted through RQ produce the same result — the actual proof that background-job execution holds for the simplest case.

### Country catalog seed pipeline

- **Auto-derived** from Natural Earth admin-0 geometry (ported `load_ne_countries`, extended to keep per-country `geometry.bounds`, not just unioned region geometry): `bbox_json`, and `srtm_bbox_json` (bounds rounded outward to whole degrees, reshaped into the `[min_lat, max_lat, min_lon, max_lon]` list format `Region.srtm` uses today — sanity-checked against Belgium's hand-typed `[49, 52, 2, 7]`; bounds are of the *mainland* = largest part plus parts within 5° (`services.overlap.mainland`), so Corsica/Sicily/Balearics count but overseas territories do not).
- **Curated, not derivable**: `countries/curated/countries.yml` — one entry per iso2 (the 49-code set) with `geofabrik_path`, `wof_code` exceptions, and `openaddresses_files`. This file defines which countries Phase 0 knows about — not the whole world.
- Geometry itself is written as GeoJSON under `library/country-geometry/<iso2>.geojson` (establishing the `library/<asset_type>/<scope>/…` convention early), with the path stored as `ne_geometry_ref`.
- `seed_countries(session)` is idempotent (upsert keyed on iso2, logs changed fields on re-run), invoked via `flask seed-countries` — reusing the Flask app's own config/DB bootstrap rather than a second one, and establishing the CLI-command convention later phases (GC/retention jobs) will reuse.

### Alembic/SQLAlchemy notes

Models live in a **package** (`models/country.py`, later `models/config.py`, `models/download.py`, …), not one file — the full schema is ~16 tables across all phases. **Phase 0 migrates only the `country` table**, not the full DESIGN.md schema upfront: each later phase already owns "define the tables it needs" in the Modular Development Plan, and pre-declaring later tables now risks schema drift before the consuming code (e.g. the map UI's real needs) is written. Set `render_as_batch=True` in `migrations/env.py` now — cheap today, and SQLite's limited `ALTER TABLE` support will require it for nearly every migration from Phase 1 onward. WAL mode applied via a single `event.listens_for(engine, "connect")` PRAGMA hook in `db.py`, shared by Flask, the worker, and Alembic.

### Phase 0 acceptance checklist

1. `flask run` boots; `GET /health` returns 200.
2. `alembic upgrade head` on a fresh SQLite file creates exactly `country` + `alembic_version`; re-running is a no-op; `downgrade base` → `upgrade head` round-trips cleanly.
3. WAL mode confirmed active (`PRAGMA journal_mode` reports `wal`).
4. `run_worker.py`, run directly on the host, connects to `redis://localhost:36389/0` (the dedicated `sw-datamanager-redis` from the new `infra/data-manager` compose stack) — proving the dual-reachability requirement for local debugging.
5. A trivial `ping` job and a `NoOpStage` job both complete via RQ; the `NoOpStage` RQ result matches a direct in-process call.
6. `NoOpStage` writes its marker output under `work/<run_id>/…`.
7. DAG resolver unit tests pass (chain ordering, independent-stage scheduling, unresolved-dependency error, cyclic error).
8. Structured logs emitted by both the Flask process and the RQ worker.
9. Each error-taxonomy exception type is exercised in a unit test.
10. `flask seed-countries` populates a `country` row for all 49 codes referenced in `config-dev.yml`, field-by-field matching those YAML files — spot-checked first for Benelux/France/Germany, then confirmed complete for the rest; re-running is idempotent.
11. `bbox_json`/`srtm_bbox_json` spot-checked against Belgium's hand-typed `[49, 52, 2, 7]`.
12. `data-pipeline` remains untouched — Phase 0 is purely additive in the new sibling directory.

### Open risks / decisions worth a spike before implementation
1. **Country catalog curation effort** — `geofabrik_path` and especially `openaddresses_files_json` cannot be fully auto-derived from geometry; this is one-time manual/scripted curation against the OpenAddresses source index and Geofabrik's directory listing, needed before Phase 1 is usable for real regions. Scope this explicitly (which countries/subdivisions matter for SwayRider's actual coverage) rather than trying to catalog the whole world upfront.
2. **OpenAddresses per-country granularity in the UI** — default recommendation: auto-select all known files for a country when added to a region, with an optional expandable override to deselect specific ones; confirm this default is sufficient before Phase 1 build-out.
3. **Tool-build caching** — *resolved for Valhalla (2026-10-02)*: a compiled tool is a machine-local cache under `DATA_ROOT/tools/`, built from Settings → Tools, not an asset. Same approach for the Pelias repos.
4. **Pelias ES snapshot/restore boundary** — spans both build and deploy today; decide which side owns it in the new split before Phase 6.
5. **Tiles parallelism granularity in RQ** — *obsolete* (the tiles stage is a Protomaps extract, no build); remaining questions: extract size/time for the configured extent, Protomaps' terms for automated downloads, and PMTiles support in tilesservice/clients (SERVICES.md); spike before Phase 7.
6. **SQLite concurrency** under multiple simultaneous heavy jobs — WAL mode should suffice; confirm with a load test before Phase 4–5.
7. **Service-name knowledge duplication** — `deployment_target.service_map_json` needs container/service names that also live in `infra`'s Compose files. Recommendation: `infra` publishes a small explicit `service-map.yml` per environment that `data-manager` reads, keeping `infra` as the single source of truth for topology.

---

## Tiles stage: Protomaps PMTiles extract (future, Phase 7)

**Decision (2026-10-01):** the tiles stage no longer builds tiles. It **downloads a regional extract of the Protomaps daily planet build** (`build.protomaps.com/YYYYMMDD.pmtiles`) and ships it as `tiles.pmtiles`. This supersedes the 2026-09-30 decision to build OpenMapTiles-schema MBTiles with planetiler (which itself superseded the legacy three-level custom pipeline). Nothing below is built yet; the Configure UI (extent, style) already stores what the stage will consume, but parts of it need rework (see below).

### What Protomaps provides (checked live 2026-10-01)
- Daily planet builds, ~138 GB each, listed with size, md5 and b3sum at `build-metadata.protomaps.dev/builds.json` (and `maps.protomaps.com/builds/`); each build is tagged with a schema version (4.15.2 at the time).
- PMTiles v3, clustered, gzip-compressed MVT, **Z0–15**, HTTP Range supported, so the `pmtiles extract` CLI (go-pmtiles) can pull only the tiles inside a bounding box instead of the whole planet.
- **Protomaps basemap schema** (not OpenMapTiles): layers `boundaries, buildings, earth, landuse, natural, places, pois, roads, transit, water` (the file metadata was only partly read; verify), features carry `kind` / `kind_detail`, `min_zoom` and `name:xx`.
- **Not yet verified:** Protomaps' terms for repeated automated downloads, the extract size for benelux+france+germany at Z0–15, MapLibre Native's PMTiles support.

### Required build changes
- **Stage = download + extract.** `tiles` consumes no `osm-pbf` and runs no planetiler: it picks a planet build (newest by default, or a pinned/overridden version like other downloads), runs `pmtiles extract <build url> tiles.pmtiles --bbox=<region_size>` and validates the result (header: Z range, bounds, tile count; `pmtiles verify`; metadata `vector_layers`; schema version recorded). The run ends `awaiting_review` like every stage; the approved extract is the `tiles.pmtiles` asset, with the source build date and schema version in `meta_json`. Planet builds are the versioned download source (`protomaps:<YYYYMMDD>` keys); the extract, not the 138 GB planet, is what is stored and retained.
- **Tooling.** The `pmtiles` CLI replaces planetiler + JRE in `datamanager/tools.py` (download-only tool, pinned version + checksum). Neither Java, the planetiler jar, Natural Earth nor the OSM water polygons are needed for tiles any more (Natural Earth is still used for the country catalog).
- **Configuration.** *Done 2026-10-01:* the Configure → Tiles tab, its tables (migration 0018 drops `tile_settings`, `tile_override`, `tile_source_override`), `services/tiles.py` and the `region_size`/`tile_size`/`tile_regions` keys of the resolved config and the legacy YAML export are removed. A later extent extract takes its bbox from the regions' core/overlap geometry instead of a stored extent.
- **Styles.** The vendored OMT base styles and `services/map_styles.py` (label zoom patching by OMT `place` `class`) target the wrong schema. Replace them with styles generated for the Protomaps schema (`@protomaps/basemaps` themes: light, dark, white, grayscale, black; it is a JS package, so either vendor the generated style JSON per release or generate it with node at build time) and rework the Style tab to patch the `places` layers (by `kind`, minimum zoom) accordingly. The Style tab preview needs a Protomaps-schema source (a public demo/build URL or our own extract) instead of OpenFreeMap. Fonts and sprites come from `protomaps/basemaps-assets` (Noto Sans glyph PBFs, sprite sheets).
- **Levels disappear / over-zoom.** One tileset Z0–15, MapLibre over-zooms above. No `L0/L1/L2`, no per-10° cells.
- **Custom features lost** (Protomaps tiles are not ours to change): yellow motorway ramps (`motorway_link_type`), `highway_labels` A/E/N shields (use `roads` `ref`/`network` where present), the legacy `population` string, the 26-language Natural Earth country labels (use `name:xx`), Polsby-Popper forest/urban filtering. Accept the Protomaps behaviour; a custom layer would need our own planetiler build, which is the fallback if the schema ever blocks a requirement.
- **Freshness / consistency.** Tiles come from Protomaps' own daily OSM snapshot, not from our Geofabrik extracts used for routing and search, so they can differ by a few days. Acceptable; record the build date in the asset.
- **Deploy.** `deploy-tiles.sh` and `tiles.tar` → deliver `tiles.pmtiles` plus the light/dark style files into `releases/<id>/tiles/`; the active-release pointer replaces copy-and-restart; restart/reload of `tilesservice` is a declarative deploy step. Rollback = previous release. See SERVICES.md for the tilesservice and client changes (PMTiles reader or Range serving, MapLibre Native support, new style/glyph/sprite sources).
- **Licensing/attribution.** OSM data ODbL ("© OpenStreetMap contributors"); Protomaps basemap schema and styles are BSD-3 (keep their notice in `datamanager/styles/NOTICE`); no "© OpenMapTiles". The style must show the OSM attribution.
- **Retirement list.** `build-tiles`, `TilesPipeline`, `pipeline/tiles.py`, `pipeline/osm_funcs.py` (tile part), tippecanoe/GDAL tool builds for tiles, the OMT styles under `datamanager/styles/`, `tile_size`/tile grid handling, `deploy-tiles.sh` rename logic.

### Styles and label levels (Phase 1 work to adapt)
Style JSON is a build artifact derived from a vendored base style (light + dark) with the configured minimum zoom per place kind patched in. A style can only show a feature at/after the tile's own minzoom. Phase 1 built this for the OpenMapTiles schema (`services/map_styles.py`, `datamanager/styles/*.json` OpenFreeMap styles, preview from OpenFreeMap tiles); it must be ported to the Protomaps schema when the stage is built (see above). **Done (2026-10-01):** ported to the Protomaps schema: vendored `@protomaps/basemaps` flavors (light, white, grayscale, dark, black), the locality layer split per label kind, the Style tab preview over the approved `tiles:planet` file via the pmtiles protocol. Glyphs and sprites still come from the public `protomaps/basemaps-assets` pages (vendoring them for the deploy is open).

## Critical Files (reference for implementation)
- `data-pipeline/pipeline/config/region.py`, `border_region.py` — exact shape a resolved region/border-region must produce; the derivation the new `region_country`→`country` join must reproduce.
- `data-pipeline/pipeline/generate_polygons.py` — Natural Earth country loading (`load_ne_countries`) and the overlap/border buffer-and-intersect logic the map UI's country layer and border-region validation build on.
- `data-pipeline/config/config-mini.yml` — concrete evidence of the OpenAddresses-list duplication the country catalog eliminates (compare Germany's `overlap.openaddresses` block across the `benelux`/`france`/`germany` region entries).
- `data-pipeline/pipeline/manifest/manifest.py` — today's per-pipeline YAML manifest; the model `build_run`/`build_step`/`asset` replace.
- `data-pipeline/pipeline/publish.py` — the flawed cross-manifest-tag assembly logic being replaced by `assembly`/`assembly_item`.
- `data-pipeline/pipeline/tiles_pipeline.py` + `pipeline/tiles.py` — largest, highest-risk stage; source of the ordered `Step` contract model.
- `data-pipeline/pipeline/osm_pipeline.py`, `border_pipeline.py`, `border_crossing.py`, `valhalla_pipeline.py`, `pelias_pipeline.py` — the four other stage runners to port.
- `infra/dev/scripts/deploy-all.sh` + sibling `deploy-*.sh`, `infra/dev-mini/scripts/deploy.sh`, `infra/dev/scripts/lib.sh` — deployment logic the `DeployTarget` abstraction replaces.
- `infra/dev/layer-00/compose.yaml` — reference pattern (`redis:7-alpine`, host-mapped port, ephemeral config) for the new `infra/data-manager/compose.yaml`'s dedicated Redis service.

## Verification approach (per phase)
- **Phase 0**: unit tests for the stage-contract DAG resolver against synthetic stage definitions; country catalog seed pipeline produces a `country` row for every country referenced in today's `config-dev.yml`/`config-mini.yml`, cross-checked field-by-field against those files' hand-typed values; Alembic migration applies cleanly to a fresh SQLite file.
- **Phase 1**: build a region through the map UI matching one of today's hand-written regions (e.g. `benelux`) and confirm the resolved `config_resolved.resolved_json` is equivalent (same core/overlap OSM paths, WOF codes, OpenAddresses files, SRTM bboxes) to the hand-written YAML it replaces — this is the acceptance test for the whole phase.
- **Phase 2**: OSM runner output for that same region matches (byte-for-byte or hash-equivalent) a run of the current `prepare-source-data` script against the equivalent hand-written config.
- **Phase 3**: publish + deploy the Phase 2 OSM asset to a real `dev-mini` environment, confirm it lands correctly with no restart needed, then roll back and confirm the previous state (or absence) is restored — first true end-to-end loop.
- **Phases 4–7**: each ported stage runner must produce output semantically equivalent to the corresponding old CLI script run against an equivalent config, checked once per stage before its old script is retired; deploy extended and verified against a real environment each time.
- **Phase 8**: full end-to-end run (configure via UI → download → all 5 builds → assembly → publish → deploy) on `dev-mini` with zero manual steps and zero legacy scripts remaining, compared against a final legacy run for parity before old code is deleted.

---

## Locality boundaries — change list for the Pelias stage (Phase 6)

Catalog side (implemented): per-country **boundary sources** in `country_boundary_source` (migration 0005), registry in `datamanager/boundary_sources/`: `osm_admin` (OSM `admin_level`s holding sub-municipal localities, e.g. BE `[9]`, NL `[10]`, LU `[9]`, DE `[9, 10]`), `official_polygons` (URL + format `geojson|ogc-api|shapefile-zip` + name field), `geonames_postal` (postcode → place name). Nothing consumes these yet. **Default = no source: Who's On First (WOF) boundaries are used unchanged.** Configured in the country modal (right-click on the map).

### Why (root cause, verified against the April 2026 Pelias build data)
`Oosthamsesteenweg 8` (Olmen, Balen) comes back as *Kwaadmechelen*. BOSA (OpenAddresses) and OSM both say city Balen, postcode 2491, so the wrong town is added by Pelias' admin lookup from WOF polygons: WOF locality 101913527 "Kwaadmechelen" (Quattroshapes geometry, ~1 km²) is mislocated on Olmen, while WOF's real "Olmen" locality (1125872719) is a *Point* without polygon and can never match a point-in-polygon lookup. WOF postalcode 2491 has no coordinates (0, 0), so `usePostalCities` cannot compensate. OSM has the right polygon: r3369711 `Olmen`, admin_level 9, `ref:INS=13003B`, containing the address point; OSM's own Kwaadmechelen (r3895749) does not.
**Acceptance test for the stage:** geocoding `Oosthamsesteenweg 8` yields locality *Olmen*, localadmin *Balen*, region *Antwerpen*.

### Sub-municipal level differs per country, so it is configured, never guessed
Boundary relations per `admin_level` in the local Geofabrik extracts: BE L9=2706 (deelgemeenten); NL L10=2535 (woonplaatsen); LU L9=223; DE L9=10167 and L10=8746 (Ortsteile); FR L9=3912, L10=1514; MC L10=9. Overpass samples: IE L10≈51k are townlands (not localities), AT L10≈5.2k cadastral communes, CH L8≈2.1k municipalities.

### Changes needed when the Pelias stage is implemented
1. **WOF patch step** (explicit, versioned `build_step`; consumes `osm-pbf`, produces asset `wof-patched-sqlite`): for each country with an enabled boundary source, build locality polygons — OSM: `osmium tags-filter r/boundary=administrative` limited to the configured levels + `osmium export` from that country's PBF (multipolygons need a seekable file); official: download + normalise, taking the place name from `name_field`. Derive each polygon's WOF hierarchy (localadmin/county/region/country ids) by centroid containment against the WOF admin polygons, then write records into the WOF SQLite DBs Pelias reads (`spr`, `geojson`, `ancestors`, `names`) under a reserved id range with `src:geom` = `osm`/`official`, and remove/deprecate that country's existing WOF localities (policy: the configured source replaces all WOF localities of the country). Untouched for countries without a source.
2. **Postal names:** enabled `geonames_postal` → import postcode → place name (e.g. BE 2491 = Olmen, 3945 = Kwaadmechelen/Ham/Oostham) for `usePostalCities` and as postcode→place fallback where no polygon matches. (GeoNames publishes 121 postal files; list in `boundary_sources/geonames_postal.py`.)
3. **Wiring:** point `imports.whosonfirst.datapath` (importer and the PIP service used by `adminLookup`) at the patched SQLite DBs; re-import ES and switch the alias as for other Pelias data.
4. **Open questions to resolve then:** how the Pelias PIP service / WOF importer treat non-WOF ids; polygons that span two parent municipalities; licences and attribution (OSM ODbL; each official source); whether ES needs `locality` records for Olmen-style deelgemeenten or only `localadmin`.
5. **Official-source candidates** (reachable 2026-09-30, quality and licence *not verified*): NL PDOK BAG `woonplaats` OGC API (GeoJSON), BE Statbel statistical sectors, LU data.public.lu API. FR (`geo.api.gouv.fr`, IGN Admin Express) and DE (BKG) unconfirmed. No defaults are pre-filled for `official_polygons`.
