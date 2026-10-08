# RELEASE-CONTRACT.md — packages, deploy configurations, deploys

Handover spec for the **publish/deploy side** of data-manager (DESIGN.md Phase 3, migration step A4). **Implemented on the server where data-manager runs** so it can be tested there; this machine only records decisions. Code state read 2026-10-05: stages and assets exist (migrations 0001–0018); the `repo` and `deploy` sidebar pages are empty stubs (`blueprints/repo/routes.py`, `blueprints/deploy/routes.py`); there are **no** `assembly` / `publish_record` / `deployment_*` tables yet; `releases/` and `deploy-state/` exist under `DATA_ROOT` but are empty.

Context and the per-service/target side: [`../Docs/MIGRATION-DATA-MANAGER.md`](../Docs/MIGRATION-DATA-MANAGER.md) (§3 deployment strategy, Phase A4), tilesservice contract in `SERVICES.md` and `TILESSERVICE-PMTILES.md`.

## 1. Concepts and decisions (2026-10-05)

Three separate things, each testable alone:

| Concept | What it is | UI | Replaces (DESIGN.md) |
|---|---|---|---|
| **Package** | An immutable, tagged archive of one consistent build: approved assets of every artifact class + `package.json` manifest. Kept in the package repository. | `/repo` | `assembly`, `assembly_item`, `publish_record` |
| **Deploy configuration** | A named, pluggable description of *where and how* packages are put into service: a **driver** type + settings. First driver: `compose-single-machine` (dev-mini). Later maybe more (k8s, …). | `/deploy` → Configurations | `deployment_target` (+ `DeployTarget` abstraction) |
| **Deploy** | An action: **package tag + deploy configuration** → transfer, verify, activate (and later rollback). One record per execution. | `/deploy` → Deploys | `deployment_record` |

Decisions:
- Packaging knows nothing about targets; drivers know nothing about stages. The only shared interface is `package.json` + the files.
- The old "`LocalDeployTarget` first / `SSHDeployTarget` later" and the 2026-10-05 note "`RemoteCopyDeployTarget`" are folded into the **driver** model: local vs ssh is a setting of the `compose-single-machine` driver (a local copy is the same code with a local path).
- Rollback is not a special path: deploy an older package tag with the same configuration (DESIGN.md principle kept).
- "The repo" = data-manager's own **package repository** (`DATA_ROOT/releases/<tag>/` + DB rows), not a git repository. *(Assumed; confirm — see §7.)*

## 2. Package

### 2.1 Content (per artifact class)
Classes are the unit a deploy config maps to a drive/target. Sources are existing asset types:

| Class | Part contents (per region unless noted) | Source asset types / downloads |
|---|---|---|
| `tiles` | `tiles.pmtiles`, `styles/…`, `glyphs/…`, `sprites/…`, tiles `manifest.json` (tilesservice contract in `SERVICES.md`) | download version `tiles:planet` (**a download record, not an asset** — see §7), `style` assets; glyphs/sprites: vendored in the app (`datamanager/blueprints/configure/static/map-assets`), `manifest.json`: generated at packaging |
| `valhalla` | `valhalla_tiles.tar` (renamed from `tiles.tar`), `admin.sqlite`, `tz_world.sqlite` | `valhalla-tiles`, `valhalla-admin`, `valhalla-timezones` (`valhalla-polylines` is a pelias input; not shipped *(verify)*) |
| `pelias` | ES snapshot, `pelias.json`, `wof/` (sqlite dir incl. patched DBs), `interpolation/{street.db,address.db}`, the Placeholder store (once, not per region) | `pelias-index-snapshot` (part meta carries `index_name`, `snapshot_name`, `snapshot_repository`, `docs` for the restore), `pelias-config`, `pelias-wof`, `pelias-interpolation-street-db`, `pelias-interpolation-address-db`, download `placeholder:store` |
| `geodata` | `manifest.yml` (regionservice format, generated at packaging: `regions.<r>.contour.{core,extended}` and `shared.border-crossings.<pair>`, each `{local-file, remote-file, hash-type: md5, hash}`, paths relative to the release), contour GeoJSON, border-crossing CSVs | `region-outline`, `border-crossings` *(checked against regionservice `internal/geodata` 2026-10-07: only `regions.<r>.contour.{core,extended}` and `shared.border-crossings.<pair>` with `remote-file` (relative to the geodata dir) are read; `path`, `tag`, the timestamps, `local-file` and the hashes are ignored, hashes are never verified. The region keys must equal the `from_region`/`to_region` values of the crossing CSVs, because regionservice looks crossings up by those names: both are the lowercase region slugs)* |

A package may contain all classes or a subset (`package.json` lists them); a package of subset classes is valid, **but** packaging validates cross-class consistency when classes overlap: the same region set in `valhalla`, `pelias` and `geodata`, one configuration/resolved-hash, all source runs `approved` (only approved assets feed packages, as for stages).

### 2.2 Archive form
- The package **owns its files**: parts are **copied** from `library/`/`downloads/` into the package repository (decision 2026-10-05, replaces "hard-linked or copied"). The repository is its own folder, `PACKAGE_ROOT` (env, default `DATA_ROOT/releases`; on this machine `/mnt/hdd-pool/swayrider/data-repo`, a ZFS dataset), on a different filesystem than `DATA_ROOT` (XFS on `/mnt/ssd2`), where hard links are impossible. Same filesystem: `os.link` is allowed as an optimisation (copy on `EXDEV`). Each file is copied and hashed in one pass (sha256, compared with the known `asset.content_hash`/download hash), so asset/download cleanup can never break a package and needs no "referenced by a package" protection. Every package costs its full size, hence the free-space pre-check (size x 1.05) and manual retention (§2.4). Recommended dataset properties: `compression=lz4` or `off` (parts are already compressed), `recordsize=1M`.
- Recommended form: `releases/<tag>/package.json` + per class either a directory of files or one `<class>/<part>.tar` for many-small-file parts (wof sqlite dir, geodata, styles/glyphs). **Single huge already-compressed files (`tiles.pmtiles`, ES snapshot, `valhalla_tiles.tar`) are stored as-is, not re-tarred** (no gain; keeps rsync resume and range reads). Exact form: open decision §7.
- Layout (`$PACKAGE_ROOT/<tag>/`; `<tag>.partial/` while building, swept on failure and at start, never counted as a package):
```
<tag>/
  package.json                  manifest (below); written last; its presence = package complete
  tiles/        tiles.pmtiles, manifest.json (generated), styles/<id>/<version>/{light,dark}.json, glyphs/<fontstack>/<range>.pbf, sprites/<name>[@2x].{json,png}  (plain files: the S3 release needs single objects)
  valhalla/<region>/  valhalla_tiles.tar, admin.sqlite, tz_world.sqlite
  pelias/<region>/    <region>.es-snapshot.tar, pelias.json, wof.tar.gz, interpolation/{street.db,address.db}
  pelias/placeholder/ store.sqlite3.gz  (the deploy unpacks it to placeholder/data/store.sqlite3)
  geodata/      contours/<region>-{core,extended}.geojson, border-crossings/<a>-<b>.csv   manifest.yml (generated)
```

### 2.3 `package.json` (schema 1)
```json
{"schema": 1, "tag": "r-20261005-1", "created_at": "2026-10-05T12:00:00Z", "created_by": "…",
 "config": {"name": "dev-mini", "resolved_hashes": {"benelux": "…", "france": "…", "germany": "…"}},
 "regions": ["benelux", "france", "germany"],
 "tool_versions": {"valhalla": "…", "elasticsearch": "…", "osmium": "…", "pmtiles": "…"},
 "classes": {
   "tiles": {"parts": [{"path": "tiles/tiles.pmtiles", "kind": "file", "size": 0, "sha256": "…",
                        "source": {"download_id": 0, "source_key": "tiles:planet", "version": "20261004"},
                        "meta": {"schema_version": "4.15.2", "date": "2026-10-04"}}]},
   "valhalla": {"parts": [{"region": "benelux", "path": "valhalla/benelux/valhalla_tiles.tar", "kind": "file",
                           "size": 0, "sha256": "…", "source": {"asset_id": 0, "run_id": 0}}]}}}
```
Rules: sha256 per file (a `tar` part hashes the tar); `source` ids give the traceability join (package → asset → run → download) of DESIGN.md; the file is written after all parts are verified, atomically; a package is never modified afterwards. The tiles class additionally carries the tilesservice `manifest.json` (SERVICES.md schema) inside its part directory; the deploy copies it as part of the tiles release.

### 2.4 Tags, labels, retention, repository

Every package carries the fixed tags `date` (UTC, `YYYY-MM-DD`) and `config` (configuration name); the new-package form shows them up front. They and the other automatic keys are reserved: labels cannot set or overwrite `date`, `config`, `regions`, `classes`, `created_by`, `source_runs`, `tiles_build`, `tool.*`, `resolved_hash.*` (`packages.check_labels`). **Automatic tags** are frozen at packaging time, written into `package.json` (`tags` object) and mirrored in the table `package_label` (`origin = auto`) for filtering: `config` (name and id), `resolved_hash` per region, `regions`, `classes`, `created_at` (UTC), `created_by`, per class the data dates and tool versions (planet OSM date, Protomaps build date, Pelias run ids, valhalla tag, elasticsearch version, pelias ref, `PLAN_VERSION`) and the source run ids. **User labels** are mutable (`origin = user`): free `key=value` or bare labels (`candidate`, `live=dev-mini`), a `note` and `protected`. They live in the DB and are re-exported to `labels.json` next to `package.json` (outside the hashed content, so the package stays immutable). **The folder is self-describing**: `flask packages-reindex` rebuilds the `package`, `package_item` and `package_label` rows from `package.json` + `labels.json`, so the repository can move to another machine or survive a lost DB. Tables (migration 0019): `package(id, tag UNIQUE, config_profile_id, status building|complete|failed, size_bytes, path, note, protected, created_at, created_by, build_run_id, package_json_hash)`, `package_item` as in §6, `package_label(package_id, key, value, origin)`. Packaging is a `package` stage run (RQ, byte progress) **without a review gate** (a package is not input of another stage); the package itself is the artefact and `verify` is the check.

- Tag `r-YYYYMMDD-N` (N = counter per day), unique, immutable; optional free-text note and `protected` flag (never auto-pruned). Delete only if no deployment references it as current or previous (label `live=<config>`).
- **Extra tags (user labels)** are added, changed and removed one by one on `/repo/<tag>` (`packages.add_label|change_label|remove_label`). A bare label (`v1.0.1`, `test-20261007`) names one package: it is unique across the repository and never equals another package's tag, so `deploy --tag <label>` is unambiguous; `key=value` labels may repeat.
- **Retention in the repository is manual** (decision 2026-10-08): nothing deletes packages by itself. `packages-prune` is an explicit CLI command (dry run unless `--apply`); a package that is live on a deploy config can never be deleted or pruned. Disk matters: each package with tiles is ~140 GB; two planet builds already in `downloads/` (`download.tiles_keep`) — set `tiles_keep = 2` while rollback of tiles is wanted, but packages hold their own hard links so the download prune is independent.
- Create from the `/repo` page ("Package current approved build" for a configuration; list/inspect/verify/delete/protect). Creation is an RQ job (same `build_run`/`build_step` progress infrastructure, stage key `package`) since hashing 140 GB takes time.

## 3. Deploy configuration

Stored in a table `deploy_config(id, key, driver, description, config_json, created_at, updated_at)`, edited in `/deploy`; **secrets are never stored** (ssh key path / agent only, referenced by path). Validated by the driver on save (`validate_config`).

### 3.1 Driver interface (core orchestrates, driver executes)
```
validate_config(cfg) -> [problems]
describe_state(cfg) -> {class: {current_tag, previous_tag, releases: [...]}}   # read from the target, not from a cache
plan(cfg, package, classes) -> [steps]                                         # shown before executing; includes what is already on the target (skip)
transfer(step) ; verify(step) ; activate(step) ; finalize(step)                # per class; resumable; progress via step_cb
rollback_hint(cfg, class) -> previous tag                                      # rollback itself = deploy(older tag)
```
The orchestration (ordering across classes, `deployment` record, RQ run with `build_step` rows, error taxonomy, partial-failure bookkeeping) lives in core and is driver-independent; adding a k8s driver must not touch it.

### 3.2 Driver `compose-single-machine` (first; dev-mini test-bed)
Config sketch (per class its own root = its own drive; `host: null` = local):
```json
{"driver": "compose-single-machine",
 "host": {"ssh": "deploy@dev-mini", "port": 22, "key": "~/.ssh/dm_deploy"},
 "classes": {
   "tiles":    {"transport": "s3", "…": "see §3.3", "activate": {"type": "tilesservice-env", "env_file": "…/layer-20/tiles-release.env", "compose": "…/layer-20/compose.yml", "service": "tilesservice"}},
   "valhalla": {"root": "/mnt/ssd-b/swayrider/valhalla",  "activate": {"type": "compose-restart", "file": "…/layer-10/docker-compose.yml", "services": {"benelux": "…", "france": "…", "germany": "…"}}},
   "pelias":   {"root": "/mnt/ssd-b/swayrider/pelias",    "es_snapshots": "/mnt/ssd-c/swayrider/es-snapshots",
                "activate": {"type": "pelias-restore", "es_url": "http://…:9200", "restart": {"…": "…"}}},
   "geodata":  {"root": "/mnt/ssd-b/swayrider/geodata",   "activate": {"type": "compose-restart", "file": "…/layer-20/docker-compose.yml", "services": {"all": "regionservice"}}}},
 "activation_order": ["geodata", "valhalla", "pelias", "tiles"]}
```
Procedure per class (as migration doc §3, now driver code):
1. `transfer`: `rsync -a --partial --inplace`-style resumable copy of `releases/<tag>/<class>/` to `<root>/releases/<tag>.partial/` (ssh, or local path); files already identical (size+hash) are skipped, so re-running a failed deploy resumes.
2. `verify`: sha256 of every part on the target against `package.json` (`sha256sum` over ssh; for 140 GB this is a conscious cost — a `verify: size|full` setting, default `full`); mismatch = failed deploy, nothing switched.
3. `finalize`: `mv <tag>.partial → <tag>`, then switch `<root>/current` with a **relative** symlink (`ln -sfn releases/<tag> current.new && mv -T current.new current`); remember the previous target.
4. `activate`: class-specific action; **health check** (HTTP/`/ready`/ES query) with timeout; on failure automatic switch back of `current` + re-activate previous, deploy marked `failed` with reason.
5. Prune (decision 2026-10-08): a target holds **only `current` and `previous`**. After the new release is healthy, every other release directory (older releases, leftover `.partial` folders) is removed; the old `current` becomes `previous`, the old `previous` is dropped. Before the health check passes nothing is removed, so a failed deploy leaves the target as it was. No `keep_releases` setting. Pelias indices of removed releases are deleted from Elasticsearch with them.
6. **Rollback** (`current` back to `previous`): flip the symlink, re-activate, health check; then the release that was rolled back is removed from the target. Afterwards there is no `previous` until the next deploy. The removed release is still in the repository and can be deployed again from there.
Activation per class: tiles = upload to the S3 release, write `current.json` last, write `PMTILES_URL` into `tiles-release.env` and force-recreate tilesservice (until it reloads on `current.json` itself); valhalla = per-region container restart; pelias = unpack the snapshot into `es_snapshots/<tag>/<region>`, register repository `dm_<tag>_<region>`, restore the index (skipped when present; `pelias.json` pins the concrete index name, so there is no alias switch), flip `current`, restart pip/api/interpolation; geodata = restart regionservice. Order and rationale in migration doc §3 (region names must agree across classes).

State truth is on the **target** (`current` symlinks); `deploy-state/<config key>/state.json` is only a cache refreshed by `describe_state`.

### 3.3 `s3` transport of the `compose-single-machine` driver (tiles class; decision 2026-10-05)
A class target may set `transport: "s3"` instead of the default `rsync`; first use is the `tiles` class, whose release lives in the Garage bucket (`../Docs/MIGRATION-DATA-MANAGER.md` §3.1a).
```json
"tiles": {"transport": "s3", "endpoint": "https://s3.dev-mini.example", "region": "garage", "bucket": "swayrider-tiles",
          "credentials": {"access_key_env": "DM_S3_ACCESS_KEY", "secret_key_env": "DM_S3_SECRET_KEY"},
          "ready_url": "http://tilesservice.internal/v1/tiles/ready"}
```
- `transfer`: upload `releases/<tag>/…` objects (multipart for large parts; resumable by skipping objects whose size and recorded checksum already match); the object key layout is the tiles contract in `SERVICES.md`.
- `verify`: size plus checksum (S3 additional SHA-256 where the store supports it, else a `sha256` object metadata value written at upload; optional full read-back, same cost trade-off as §7.6).
- `finalize`: write `current.json` **last** (single PUT = atomic switch); keep the previous pointer value for rollback.
- `activate`: none; `tilesservice` polls the pointer. Health check: the service's readiness endpoint reports the new release id within a timeout, otherwise the pointer is written back (automatic rollback).
- Retention as §3.2 step 5: only the `current` and `previous` release prefixes stay.
- Credentials come from the environment of the data-manager process, never from the package or the stored configuration; the key needs write access to this bucket only.
- Rollback: deploy the previous tag (objects are usually still there; the transfer step then skips everything and only rewrites the pointer).

### 3.4 Later drivers
Anything that can map the same package parts onto its own storage and activation (e.g. k8s: PVC/object-store upload + rollout restart). Out of scope now; the interface in §3.1 must not assume symlinks or rsync.

## 4. Deploy (record and semantics)
`deployment(id, package_id→, deploy_config_id→, classes_json, status[running|succeeded|failed|rolled_back], package_tag (kept when the package is deleted later; `package_id` becomes NULL), previous_package_id→ (per class in `detail_json`), started_at, finished_at, triggered_by, build_run_id→, detail_json)`.
- Inputs: package tag + deploy config key + optional class subset (**partial deploys allowed**; the cross-class consistency warning is shown when the resulting live set mixes tags).
- Steps run in `activation_order`; a failed class stops the sequence; classes already activated stay (state is visible in `describe_state`), the deploy is `failed` with per-class results; re-running resumes.
- Rollback: UI/CLI = `current` back to `previous` (§3.2 step 6); recorded as a new deployment with `rolled_back_from`.
- Settings group `deploy`: `deploy.verify` (`full` default | `size`), `deploy.health_timeout` (seconds). Migration 0021 creates `deploy_config` and `deployment`; `DeployError` is the error type.
- Concurrency: one running deploy per deploy config (lock row / RQ single worker queue).
- Traceability: `deployment → package → package_item → asset/download → build_run → config` (the existing one-join story).

## 5. Target-side helper (dev-mini `deploy.sh`)
No agent on the target: the driver only needs ssh, `rsync`, `sha256sum`, `ln`, `mv`, `docker`/`docker compose`. The planned `infra/dev-mini/scripts/deploy.sh copy|activate|rollback|list` stays as a **manual fallback** (same layout, same `package.json` verification) for an operator without data-manager; it is *not* required for the data-manager flow. Keep both consistent through the layout in §2.2/§3.2.

## 6. Server implementation checklist (data-manager)
Order is chosen so each step is testable with **fixture packages** (a few KB per part; no planet, no Valhalla):
1. **Alembic migration 0019:** `package`, `package_item`, `deploy_config`, `deployment`; no change to existing tables. (`package_item(package_id, class, region, path, kind, size, sha256, asset_id?, download_id?, meta_json)`.)
2. **`services/packages.py`:** collect approved assets per class (reuse `assets.current` per config/type/region), consistency checks, hard-link/copy into `releases/<tag>/`, hash, write `package.json` last; `verify_package`; delete/protect/retention; extend cleanup protection ("referenced by a package") in `services/downloads.py` and `services/assets.py`. Tag counter. Tests: fixture assets → package; tamper → verify fails; incomplete → refused; unapproved → refused.
3. **Packaging job:** stage-like `package` runner (RQ, `step_cb` progress), registered in `stages/builtin.py`; `/repo` page: list, create, inspect (parts, sizes, sources), verify, protect, delete.
4. **`deploy/` package** (`datamanager/deploy/`): `base.py` (driver ABC, §3.1), `registry.py`, `compose_single_machine.py` with the `rsync` and `s3` transports (§3.4), `orchestrator.py` (§4). Tests for `s3` against a local fake S3 or Garage in a container. Tests with a **local-path** target in a temp dir (no ssh): transfer/resume (kill mid-copy), hash mismatch, atomic symlink flip (relative), rollback to previous, retention prune, activation hooks mocked (record calls, order), health-check failure → automatic switch-back.
5. **`/deploy` page:** configurations CRUD with `validate_config`, "plan" preview, run deploy, per-class progress (reuse run/step SSE UI), history, rollback button, current state per class (`describe_state`).
6. **Server smoke test (the real one):** package a small real build (one tiny region, no planet: tiles part omitted or a small test PMTiles), deploy to a local-path root, then via ssh to dev-mini, `current` flips, activate each class, rollback drill. Only then: planet + all regions.
7. **Styles stage growth — built:** the `styles` stage writes template styles (`{{.TilesBaseURL}}`, `{{.Tileset}}`; tiles source `tiles`+`maxzoom 15`, glyph and sprite URLs on tilesservice) with an id and name (Settings → Map style in the tiles release) and an integer version (unchanged content keeps it, a change raises it; per style id). The tiles class packages `styles/<id>/<version>/{light,dark}.json`, the vendored glyphs and sprite sheets, and a generated `manifest.json` (`packages.tiles_manifest`: tileset build/date/schema from the `tiles:planet` run, styles with variants, first = default). One style per configuration for now; several named styles later.
8. **Tools requirement:** `rsync`/`ssh` already registered as required tools (DESIGN.md); also `sha256sum` on the target (document in dev-mini README).
Update `DESIGN.md` Phase 3, `CLAUDE.md` and this file's "code state" line when each step lands.

## 7. Open decisions (recommendation first)
1. **"Repo" = data-manager package repository** (`releases/` + DB), not git. *Recommend yes.* If git-tagged metadata is also wanted, export `package.json` into a git repo later.
2. **Archive form:** per-class dirs/tars with huge single files stored raw (§2.2), vs. one tarball per class vs. one tarball per package. *Recommend §2.2.* Tarring 140 GB tiles gives nothing and breaks resume.
3. **`tiles:planet` is a download record, not an asset.** *Recommend:* package items may reference a `download_id`; planet files protected from `tiles_keep` pruning through the hard link. Alternative: register the approved planet as an asset (heavier change).
4. **Deploy config storage:** DB table edited in UI (§3) vs. YAML files in the data-manager repo. *Recommend DB* (UI-first, like the rest); export/import as JSON.
5. **Two planets on disk / repository filesystem** — decided 2026-10-05: the repository is a separate folder (`PACKAGE_ROOT`, a ZFS dataset), packages are copies (§2.2); target hosts hold current + previous (~280 GB tiles).
6. **Verification cost:** full sha256 of a 140 GB file on the target after each deploy (minutes on SSD). *Recommend* full by default, `size` mode for dev.
7. **Pelias ES restore** needs the target's ES to see the snapshot repository path (`ES_SNAPSHOTS_PATH`) and the snapshot repo registered; whether the driver registers it or the infra does. *Recommend infra registers once, driver only restores.*
8. **Service map** (compose file + service names per region) lives in the deploy config; infra's inconsistent names (e.g. `sw-dev-germany-pip`) must be fixed or mapped explicitly.
9. **Auth for the ssh user** and whether data-manager runs on the target itself (then `host: null`, local paths) — both supported by the same driver.

## 8. Cleanup after packaging (decided 2026-10-05)

Purpose: keep the SSD (`DATA_ROOT`) small once a package holds the results. After a package is **complete and verified** the `/repo` package page (also reachable from Build → Downloads) offers a cleanup dialog: one checkbox per category, **prefilled with the defaults below**, a dry run listing files and sizes, then delete. Defaults are settings `cleanup.default.<category>` = `delete|keep` (editable in Settings and in the dialog). Registry: `services/cleanup.py` (category → selector → files/rows + size), reusing the dry-run, pin and in-use logic of `services/downloads.py` and `services/assets.py` (`prune`, `_remove_files`).

| # | Category | Where | Default |
|---|---|---|---|
| 1 | Planet OSM download (`planet:osm`) | `downloads/planet` | keep |
| 2 | Protomaps tiles download (`tiles:planet`) | `downloads/tiles` | keep |
| 3 | Per-country PBFs (`country-pbf`) | `library/assets/country-pbf` | delete |
| 4 | Region PBFs (`osm-pbf`, `osm-core-pbf`) | `library/assets/osm-*` | delete |
| 5 | Valhalla outputs (`valhalla-*`) | assets | delete |
| 6 | Pelias index snapshots, current and superseded | assets | delete |
| 7 | Pelias WOF (`pelias-wof`, `wof-patched-sqlite`, incl. unapproved) | assets | delete |
| 8 | Interpolation DBs (`street.db`, `address.db`) | assets | delete |
| 9 | Small derived assets (polygons, borders, crossings, outlines, `pelias-config`, styles) — **never the manually drawn `carve-polygon`** | assets | delete |
| 10 | SRTM unpacked for Valhalla | `library/srtm/<run>` | delete |
| 11 | SRTM downloads (`srtm:*`) | `downloads/srtm` | keep |
| 12 | Pelias source downloads (WOF, GeoNames, OpenAddresses, placeholder) | `downloads/…` | delete |
| 13 | Overture CSVs and GTFS zips | `downloads/{overture,gtfs}` | delete |
| 14 | Older download versions beyond `*_keep` | `downloads/*` | delete |
| 15 | Rejected/failed run leftovers (`produced` assets of rejected runs, `work/<run>`) | `library`, `work` | delete |
| — | Natural Earth, country geometry, autofill, `.poly` files (16) and `tools/` (17) | | **not in the dialog; never touched** |

Rules: never delete a pinned or in-use (running/queued) download or asset, nor the versions that `*_keep` protects; deleting an approved asset marks the row `purged` (file gone; row, hashes and `source_download_ids` stay for traceability); stage status and fingerprints treat `purged` as "packaged in `<tag>`, rebuild on demand", not as "outdated" or failed; a stage whose input is purged refuses to start with a hint to rebuild the upstream stage. Sizes on 2026-10-05 (for orientation): downloads 252 G, library 594 G of which Pelias snapshots 454 G.

## 9. Implementation notes (as built, 2026-10-05)
- `services/packages.py` (`plan`, `create_package`, `verify_package`, `edit_labels`, `delete`, `prune`, `reindex`), tables `package`/`package_item`/`package_label` (migration 0019), `PACKAGE_ROOT`.
- Stage `package` (`stages/package.py`, `review_gate = False`: a clean run is `approved` automatically, note "automatic"); params `classes`, `labels`, `note`, `force`. Without `force` the plan is refused while a stage behind a chosen class is `outdated`, `review` or `running` (`CLASS_STAGES` in `services/packages.py`).
- CLI: `flask package-create --config NAME [--classes ..] [--label k=v] [--note ..] [--force] [--inline]`, `package-list`, `package-verify TAG`, `packages-reindex`, `packages-prune [--keep N] [--apply]` (setting `package.keep`, default 3).
- `package.json` carries `tool_versions` (valhalla tag, elasticsearch version, pelias ref, pelias `PLAN_VERSION`) next to the automatic tags.
- **Build stages** (last on the Build page, each opens a modal on Run…): `package` (current configuration, fixed tags `date` and `config`, class checkboxes, extra labels, note, "verify afterwards" on by default, live plan with blocking problems), `package-verify` (pick a package) and `cleanup` (pick a verified package, categories prefilled from Settings with sizes). All three have no review gate. Stage cards show status: Package is "outdated" when the approved results moved on from the newest package (`packages.stale_parts`), Verify is "todo" while the newest package is unverified, Cleanup is "up to date" once it ran after the newest verified package. Stages whose results a cleanup removed show "cleaned up" (not "up to date"), their consumers are blocked until the producer ran again, and a new package refuses purged parts.
- **Repo UI** (`blueprints/repo`) only shows the repository: status (folder, filesystem, free space, warnings), recent runs, the package list with filters and a verified/unverified badge, and per package the files with sources, the tags, the runs. It keeps the direct actions: edit extra labels and note, the protected flag, Delete (refused when protected) and a Verify button that starts a `package-verify` run. Verify sets `package.verified_at` (migration 0020); the cleanup needs it.
- Cleanup (stage `cleanup`: `stages/cleanup.py` runs `services/cleanup.py` per category with progress; `services/cleanup_categories.py`; CLI `flask cleanup TAG [--category K] [--apply]`): the 15 categories of §8, ticked by the Settings `cleanup.default.<category>` (group "Cleanup after packaging"). Approved assets and downloads are **purged**: the file goes, the row stays with its hash (assets: `meta_json["purged"] = {package, at}`, polygons also keep `bbox`; downloads: `purged_at`, `purged_package`), so stage statuses stay as they were; rejected leftovers and old download versions are deleted. Current assets of a packaged type and the tiles download are only removed when the given verified package holds the same asset/download id and sha256. Nothing is removed while a run is queued or running. Stages read inputs through `assets.input_path` / `downloads.input_path` and fail early with "was purged by the cleanup (packaged in <tag>): run <stage> again"; `valhalla` checks the unpacked SRTM directory.
- **Deploy driver (as built, 2026-10-08):** `datamanager/deploy/` — `base.py` (`DeployDriver`, `Activator`, `PackageView`), `registry.py`, `compose_single_machine.py` (local target only; ssh is rejected by `validate_config` for now; tiles use the object-store transport added later). Per class `plan_class` → `deploy_class` (copy into `releases/<tag>.partial`, resumable through `.deploy.json`; the source is hashed while copying/unpacking; `verify` per `deploy.verify`; rename; flip `current`/`previous`; `Activator.activate` + `check_health`; switch back on failure; `prune_class`) and `rollback_class`. Activators are looked up by `activate.type` in `base.ACTIVATORS` (`none` built in; the real ones follow). A deploy of the tag that is already current is skipped; a tag that is still on the target (previous) is only verified and switched. A failed release stays on the target (so a re-run resumes) until the next healthy deploy prunes it.
- **Activators (as built, 2026-10-08):** `datamanager/deploy/activators.py`, registered in `base.ACTIVATORS`. `compose-restart` (`services`: region -> container, or `all`; optional `ready_urls`): `docker restart` per region of the release (checked: a restart re-resolves the bind-mounted `current` symlink, so no recreate is needed), health = running and healthy, fails fast on exited/restarting containers. `pelias-restore` (`es_url`, `restart.region` templates with `{region}`, `restart.shared`): per region read `schema.indexName` from the release's `pelias.json`, skip when the index exists, else register `dm_<tag>_<region>` (readonly `fs` at `/usr/share/elasticsearch/snapshots/<tag>/<region>`), restore, restart pip/interpolation/api (+ the Placeholder when the release has it); health = index yellow and not empty, containers up. When a release is removed its indices and repositories are dropped, **except an index that a kept release still pins** (a repackaged build has the same index name in two releases). The object-store (tiles) transport and the tilesservice activator are the next step.
- **Tiles transport (as built, 2026-10-08):** `datamanager/deploy/s3_tiles.py`, used by the driver for the class `tiles` (block: `transport: "s3"`, `endpoint`, `bucket`, `region`, `credentials.{access_key_env,secret_key_env}` = names of environment variables, `activate`). Objects go to `releases/<tag>/<path without tiles/>` (manifest last), each with the package sha256 as object metadata; objects already stored with the same size and sha256 are skipped. Large files use multipart upload (64 MiB parts) and are hashed while uploading: a source that does not match `package.json` aborts the upload before it is completed. `verify` checks size and the recorded sha256 (the 140 GB are not read back). Pointers: `current.json` (`{"schema":1,"release","prefix","updated_at"}`, written after all objects) and `previous.json` (same format, the release before). The store keeps only the `current` and `previous` prefixes. Activator `tilesservice-env` (`env_file`, `compose_file`, `service`, `container`, optional `ready_urls`): writes `PMTILES_URL=s3://<bucket>/releases/<tag>/tiles.pmtiles` into the env file and runs `docker compose -f <file> up -d --no-deps --force-recreate <service>`; a failed first activation removes the env file (`Activator.abandon`). An interrupted multipart upload of a large file starts that file again (parts are not resumed). boto3 is used with path-style addressing and checksums only when required (Garage).
- **Orchestrator, stage and CLI (as built, 2026-10-08):** `deploy/orchestrator.py` (`save_config`, `plan`, `run`, `rollback`, `state`, `history`), stage `deploy` (`stages/deploy.py`, no review gate, 12 h job timeout; params `deploy_config`, `tag`, `classes`, `rollback`, `allow_unverified`), CLI `deploy-config-save KEY --file`, `deploy-config-list`, `deploy-plan`, `deploy`, `deploy-state`, `deploy-rollback` (`--inline` runs in the process, otherwise the worker). An example configuration is `deploy-configs/dev-mini.example.json` (paths to fill in; only environment variable *names* for secrets). Rules: the package is a tag or a bare label that names exactly one package, default = the newest verified package; unverified packages are refused (`allow_unverified`); classes run in `activation_order`; a failed class stops the sequence, the classes before it stay live and the `deployment` row says which failed, a re-run resumes (current classes are skipped); one running deploy per configuration (a `running` row whose run is gone is marked failed); after every deploy or rollback the label `live=<config key>` (origin `deploy`) is put on exactly the packages that are current or previous on the target, so they cannot be deleted; a rollback is a new `deployment` (`rolled_back_from`) and marks the reverted one `rolled_back`. `deploy-plan` warns when the classes would carry different packages.
- **Compose mode and the first deploy (as built, 2026-10-08):** the infra compose files do not start a service whose data directory is missing (`create_host_path: false`), so at the first deploy the containers of valhalla, pelias, geodata and tilesservice do not exist yet and `docker restart` would fail. An `activate` block with `compose_file` therefore treats its names as compose services and runs `docker compose -f <file> up -d --no-deps --force-recreate <service>`; health then looks at the container compose created. Without `compose_file` the names are container names and `docker restart` is used.
- **Deploy page (`/deploy`, as built):** configurations (JSON editor with the example prefilled, validated on save, deletable while without history), per configuration the state read from the target, a deploy form (package picker with the newest verified one preselected, classes, live plan with problems and warnings, the plan is made again on start), rollback, history; `/repo` shows `live on <config>` and disables Delete for live packages. The first real deploy and the rollback drill follow `DEPLOY-DEV-MINI.md`.
- **`ensure` (as built, 2026-10-08):** an `activate` block with `compose_file` may list `"ensure": ["pelias-libpostal"]`: compose services that must run but do not depend on a release. They are started with `docker compose up -d --no-deps <service>` before the class is activated (not recreated, so a deploy does not interrupt them).
- **Base and supporting services (as built, 2026-10-08):** the configuration may carry `"ensure": {"compose_file", "services"}` at the top (services no release depends on, e.g. `authservice`, `mailservice`: started with `docker compose up -d --no-deps` at the start of every deploy, never recreated) and an `ensure_after` block in a class's `activate` (`{"compose_file", "services"}`, compose file defaults to the block's own): services that use the class (`routerservice` after valhalla, `searchservice` after pelias), started once the class is healthy. Both only produce warnings in the run report when a service does not start; the class's own services (and `ensure` inside an `activate` block, e.g. libpostal) decide whether it succeeded.

