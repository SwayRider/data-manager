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
| `tiles` | `tiles.pmtiles`, `styles/…`, `glyphs/…`, `sprites/…`, tiles `manifest.json` (tilesservice contract in `SERVICES.md`) | download version `tiles:planet` (**a download record, not an asset** — see §7), `style` assets; glyphs/sprites: **new** (styles stage growth, §6) |
| `valhalla` | `valhalla_tiles.tar` (renamed from `tiles.tar`), `admin.sqlite`, `tz_world.sqlite` | `valhalla-tiles`, `valhalla-admin`, `valhalla-timezones` (`valhalla-polylines` is a pelias input; not shipped *(verify)*) |
| `pelias` | ES snapshot, `pelias.json`, `wof/` (sqlite dir incl. patched DBs), `interpolation/{street.db,address.db}` | `pelias-index-snapshot`, `pelias-config`, `pelias-wof`, `pelias-interpolation-street-db`, `pelias-interpolation-address-db` |
| `geodata` | `manifest.yml` (regionservice format, generated at packaging), contour GeoJSON, border-crossing CSVs | `region-outline`, `border-crossings` *(verify mapping of `region-outline` to the legacy contour files regionservice reads)* |

A package may contain all classes or a subset (`package.json` lists them); a package of subset classes is valid, **but** packaging validates cross-class consistency when classes overlap: the same region set in `valhalla`, `pelias` and `geodata`, one configuration/resolved-hash, all source runs `approved` (only approved assets feed packages, as for stages).

### 2.2 Archive form
- The package **owns its files**: parts are hard-linked (same filesystem) or copied from `library/`/`downloads/` into `releases/<tag>/`, so asset/download cleanup can never break a package. Cleanup learns "referenced by a package" (like the existing "in use" protection).
- Recommended form: `releases/<tag>/package.json` + per class either a directory of files or one `<class>/<part>.tar` for many-small-file parts (wof sqlite dir, geodata, styles/glyphs). **Single huge already-compressed files (`tiles.pmtiles`, ES snapshot, `valhalla_tiles.tar`) are stored as-is, not re-tarred** (no gain; keeps rsync resume and range reads). Exact form: open decision §7.
- Layout:
```
releases/<tag>/
  package.json                  manifest (below); written last; its presence = package complete
  tiles/        tiles.pmtiles, tiles-support.tar (styles+glyphs+sprites), manifest.json
  valhalla/<region>/  valhalla_tiles.tar, admin.sqlite, tz_world.sqlite
  pelias/<region>/    snapshot/…, pelias.json, wof.tar, interpolation/{street.db,address.db}
  geodata/      geodata.tar (manifest.yml, contours, border-crossings)
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

### 2.4 Tags, retention, repository
- Tag `r-YYYYMMDD-N` (N = counter per day), unique, immutable; optional free-text note and `protected` flag (never auto-pruned). Delete only if no deployment references it as current or previous.
- Retention: keep newest N unprotected packages (setting, default 3) + anything protected or live on a deploy config. Disk matters: each package with tiles is ~140 GB; two planet builds already in `downloads/` (`download.tiles_keep`) — set `tiles_keep = 2` while rollback of tiles is wanted, but packages hold their own hard links so the download prune is independent.
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
   "tiles":    {"root": "/mnt/ssd-a/swayrider/tiles",    "activate": {"type": "signal", "container": "sw-dev-tilesservice", "signal": "HUP"}},
   "valhalla": {"root": "/mnt/ssd-b/swayrider/valhalla",  "activate": {"type": "compose-restart", "file": "…/layer-10/docker-compose.yml", "services": {"benelux": "…", "france": "…", "germany": "…"}}},
   "pelias":   {"root": "/mnt/ssd-b/swayrider/pelias",    "es_snapshots": "/mnt/ssd-c/swayrider/es-snapshots",
                "activate": {"type": "pelias-restore", "es_url": "http://…:9200", "alias_template": "pelias-{region}", "restart": {"…": "…"}}},
   "geodata":  {"root": "/mnt/ssd-b/swayrider/geodata",   "activate": {"type": "compose-restart", "file": "…/layer-20/docker-compose.yml", "services": {"all": "regionservice"}}}},
 "activation_order": ["geodata", "valhalla", "pelias", "tiles"],
 "keep_releases": 2}
```
Procedure per class (as migration doc §3, now driver code):
1. `transfer`: `rsync -a --partial --inplace`-style resumable copy of `releases/<tag>/<class>/` to `<root>/releases/<tag>.partial/` (ssh, or local path); files already identical (size+hash) are skipped, so re-running a failed deploy resumes.
2. `verify`: sha256 of every part on the target against `package.json` (`sha256sum` over ssh; for 140 GB this is a conscious cost — a `verify: size|full` setting, default `full`); mismatch = failed deploy, nothing switched.
3. `finalize`: `mv <tag>.partial → <tag>`, then switch `<root>/current` with a **relative** symlink (`ln -sfn releases/<tag> current.new && mv -T current.new current`); remember the previous target.
4. `activate`: class-specific action; **health check** (HTTP/`/ready`/ES query) with timeout; on failure automatic switch back of `current` + re-activate previous, deploy marked `failed` with reason.
5. Prune releases beyond `keep_releases` (never the current or previous).
Activation per class: tiles = SIGHUP/poll of the symlink (no restart; tilesservice reload contract); valhalla = per-region container restart; pelias = copy snapshot into `es_snapshots`, restore, alias switch, restart pip/api/interpolation; geodata = restart regionservice. Order and rationale in migration doc §3 (region names must agree across classes).

State truth is on the **target** (`current` symlinks); `deploy-state/<config key>/state.json` is only a cache refreshed by `describe_state`.

### 3.3 Later drivers
Anything that can map the same package parts onto its own storage and activation (e.g. k8s: PVC/object-store upload + rollout restart). Out of scope now; the interface in §3.1 must not assume symlinks or rsync.

## 4. Deploy (record and semantics)
`deployment(id, package_id→, deploy_config_id→, classes_json, status[planned|running|succeeded|failed|rolled_back], previous_package_id→ (per class in `detail_json`), started_at, finished_at, triggered_by, build_run_id→, detail_json)`.
- Inputs: package tag + deploy config key + optional class subset (**partial deploys allowed**; the cross-class consistency warning is shown when the resulting live set mixes tags).
- Steps run in `activation_order`; a failed class stops the sequence; classes already activated stay (state is visible in `describe_state`), the deploy is `failed` with per-class results; re-running resumes.
- Rollback: UI/CLI "deploy previous tag" = the same deploy against the `previous` tag of that class; recorded with `rolled_back_from`.
- Concurrency: one running deploy per deploy config (lock row / RQ single worker queue).
- Traceability: `deployment → package → package_item → asset/download → build_run → config` (the existing one-join story).

## 5. Target-side helper (dev-mini `deploy.sh`)
No agent on the target: the driver only needs ssh, `rsync`, `sha256sum`, `ln`, `mv`, `docker`/`docker compose`. The planned `infra/dev-mini/scripts/deploy.sh copy|activate|rollback|list` stays as a **manual fallback** (same layout, same `package.json` verification) for an operator without data-manager; it is *not* required for the data-manager flow. Keep both consistent through the layout in §2.2/§3.2.

## 6. Server implementation checklist (data-manager)
Order is chosen so each step is testable with **fixture packages** (a few KB per part; no planet, no Valhalla):
1. **Alembic migration 0019:** `package`, `package_item`, `deploy_config`, `deployment`; no change to existing tables. (`package_item(package_id, class, region, path, kind, size, sha256, asset_id?, download_id?, meta_json)`.)
2. **`services/packages.py`:** collect approved assets per class (reuse `assets.current` per config/type/region), consistency checks, hard-link/copy into `releases/<tag>/`, hash, write `package.json` last; `verify_package`; delete/protect/retention; extend cleanup protection ("referenced by a package") in `services/downloads.py` and `services/assets.py`. Tag counter. Tests: fixture assets → package; tamper → verify fails; incomplete → refused; unapproved → refused.
3. **Packaging job:** stage-like `package` runner (RQ, `step_cb` progress), registered in `stages/builtin.py`; `/repo` page: list, create, inspect (parts, sizes, sources), verify, protect, delete.
4. **`deploy/` package** (`datamanager/deploy/`): `base.py` (driver ABC, §3.1), `registry.py`, `compose_single_machine.py`, `orchestrator.py` (§4). Tests with a **local-path** target in a temp dir (no ssh): transfer/resume (kill mid-copy), hash mismatch, atomic symlink flip (relative), rollback to previous, retention prune, activation hooks mocked (record calls, order), health-check failure → automatic switch-back.
5. **`/deploy` page:** configurations CRUD with `validate_config`, "plan" preview, run deploy, per-class progress (reuse run/step SSE UI), history, rollback button, current state per class (`describe_state`).
6. **Server smoke test (the real one):** package a small real build (one tiny region, no planet: tiles part omitted or a small test PMTiles), deploy to a local-path root, then via ssh to dev-mini, `current` flips, activate each class, rollback drill. Only then: planet + all regions.
7. **Styles stage growth** (needed by the tiles class, can follow after 1–5): named styles with versions, glyphs and sprites, tiles `manifest.json` — see `SERVICES.md` / `TILESSERVICE-PMTILES.md` §styles. Until done, the tiles class can ship only `tiles.pmtiles` + the existing two styles.
8. **Tools requirement:** `rsync`/`ssh` already registered as required tools (DESIGN.md); also `sha256sum` on the target (document in dev-mini README).
Update `DESIGN.md` Phase 3, `CLAUDE.md` and this file's "code state" line when each step lands.

## 7. Open decisions (recommendation first)
1. **"Repo" = data-manager package repository** (`releases/` + DB), not git. *Recommend yes.* If git-tagged metadata is also wanted, export `package.json` into a git repo later.
2. **Archive form:** per-class dirs/tars with huge single files stored raw (§2.2), vs. one tarball per class vs. one tarball per package. *Recommend §2.2.* Tarring 140 GB tiles gives nothing and breaks resume.
3. **`tiles:planet` is a download record, not an asset.** *Recommend:* package items may reference a `download_id`; planet files protected from `tiles_keep` pruning through the hard link. Alternative: register the approved planet as an asset (heavier change).
4. **Deploy config storage:** DB table edited in UI (§3) vs. YAML files in the data-manager repo. *Recommend DB* (UI-first, like the rest); export/import as JSON.
5. **Two planets on disk** (target: current + previous ≈ 280 GB; build host: downloads + package hard links share one filesystem or are doubled if `releases/` is on another disk). *Recommend* `releases/` on the same filesystem as `downloads/`, else copy-and-warn.
6. **Verification cost:** full sha256 of a 140 GB file on the target after each deploy (minutes on SSD). *Recommend* full by default, `size` mode for dev.
7. **Pelias ES restore** needs the target's ES to see the snapshot repository path (`ES_SNAPSHOTS_PATH`) and the snapshot repo registered; whether the driver registers it or the infra does. *Recommend infra registers once, driver only restores.*
8. **Service map** (compose file + service names per region) lives in the deploy config; infra's inconsistent names (e.g. `sw-dev-germany-pip`) must be fixed or mapped explicitly.
9. **Auth for the ssh user** and whether data-manager runs on the target itself (then `host: null`, local paths) — both supported by the same driver.
