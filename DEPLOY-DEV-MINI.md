# DEPLOY-DEV-MINI.md — first real deploy from data-manager to dev-mini

Runbook for step E: deploy a verified package to the dev-mini stack on this machine, class by class, then roll back.
The code is described in `RELEASE-CONTRACT.md` §3, §4 and §9; the target layout is in `../infra/dev-mini/README.md`.
Every step ends with something to check; stop at the first thing that does not match and report it.

## 0. What you need
- A verified package (`flask package-list`, Repo page: *verified*). Here: `r-20261007-1`.
- data-manager restarted after the merge (`./debug.sh`, worker included). Migration 0021 is applied (`alembic upgrade head`).
- The Garage read/write key in the environment of data-manager **and its worker**: `DM_S3_ACCESS_KEY`, `DM_S3_SECRET_KEY` (the `GARAGE_RW_*` values of `layer-00/.env`). The configuration only stores these *names*.
- Disk: the package is ~357 GB; each class root needs its size × 1.1 free for the first deploy, and after a second deploy the target holds `current` + `previous`. `/mnt/hdd-pool` has ~19 TB.

## 1. Prepare the host (once per machine)
0. **Shared access.** The directories are shared through a group (`swdata`, setgid, default ACL) so every administrator in it can package and deploy, and nobody locks the others out. Two scripts set this up and verify it; both show what they would do first (`--dry-run` is the default), and `--apply` asks *A* (run it, sudo asks for the password), *M* (do it yourself and press Enter) or *Q*, then checks again:
   - `scripts/init-host.sh` in data-manager: the group, you in it, the acl tools, docker access, and `PACKAGE_ROOT` as a shared directory. On a new machine run this first.
   - `infra/dev-mini/scripts/prepare-host.sh` (step 2 below): the same group for the deploy roots.
   Another administrator is added with `sudo usermod -aG swdata <name>` (they log in again, or use `newgrp swdata`, before starting `./debug.sh` or the worker).
1. In the `layer-*/.env` files set the roots: `VALHALLA_ROOT`, `PELIAS_ROOT`, `GEODATA_ROOT`, `TILES_ROOT`, `ES_SNAPSHOTS_PATH`, and the Garage data/meta paths (see each `env.example`).
2. `./scripts/prepare-host.sh --apply` in `infra/dev-mini` (run it without `--apply` first to read the plan): creates the root directories shared through the group, gives the Elasticsearch data and Valhalla scratch directories to their service uid, and reports `vm.max_map_count` and free space.
3. Start the base services: `layer-00` (Traefik, Elasticsearch, PostgreSQL, Redis, Garage) and run `./garage/smoke-test.sh`.
4. Start only `layer-00` (see above). Nothing in `layer-10` or `layer-20` has to be started by hand: the deploy creates the services that read a release (`valhalla-*`, `pelias-*`, `regionservice`, `tilesservice`), starts the base services of `layer-20` that no release depends on (`ensure`: `authservice`, `swayrider-api-register`, `mailservice`; the register job is idempotent and exits at once when the client is already registered, the volume `sw-dev-api-credentials` is created by compose), starts `pelias-libpostal` (class `ensure`) and, once a class is healthy, the services that use it (`ensure_after`: `searchservice` and `routerservice` after pelias: the router also joins the network `net-sw-dev-pelias`, which only exists once the first pelias container has been created). A supporting service that does not start is a warning in the run report, not a failed deploy. The compose files do not start a service whose data directory is missing (`create_host_path: false`), so starting them yourself before the first deploy only gives errors.

## 2. Create the deploy configuration
```
cp deploy-configs/dev-mini.example.json /tmp/dev-mini.json     # edit the /path/to/... entries
flask deploy-config-save dev-mini --file /tmp/dev-mini.json --description "dev-mini on this machine"
```
or create it on the Deploy page (the example is prefilled). The `activate` blocks use `compose_file` so the deploy creates the service containers itself. Secrets never go in the file.

## 3. Plan (changes nothing on the targets)
```
flask deploy-plan --config dev-mini --tag r-20261007-1
```
Expect per class its size, `to copy` = the full size, no problems. Problems here are paths, permissions or free space.

## 4. Deploy class by class
Start with the smallest and check after each. `--inline` keeps it in your terminal; without it the worker runs it and you follow the run page.

| Step | Command | Check afterwards |
|---|---|---|
| geodata (26 MB) | `flask deploy --config dev-mini --tag r-20261007-1 --classes geodata --inline` | `flask deploy-state --config dev-mini`: `current=r-20261007-1`; `$GEODATA_ROOT/current/manifest.yml` exists; `sw-dev-regionservice` runs and its log shows the three regions and the border crossings |
| valhalla (18.6 GB) | `... --classes valhalla` | `sw-dev-valhalla-<region>` run; a route request inside one region answers |
| pelias (~165 GB, longest) | `... --classes pelias` | indices `pelias_benelux-43` etc. in Elasticsearch (`curl localhost:39200/_cat/indices`), pip/interpolation/api containers up, a search request answers |
| tiles (~140 GB) | `... --classes tiles` | objects under `releases/r-20261007-1/` and `current.json` in the bucket; `tiles-release.env` contains `PMTILES_URL=s3://swayrider-tiles/releases/r-20261007-1/tiles.pmtiles`; a tile request via `sw-dev-tilesservice` answers |

A failed class leaves the classes before it live and the target of that class as it was (a half-copied `*.partial` stays and is resumed by the next run). The history on the Deploy page and `deploy-state` show what happened; the run page has the step log.

## 5. After the first deploy
- The Repo page shows `live on dev-mini` on the package; it cannot be deleted while it is live.
- `flask deploy-state --config dev-mini` shows `previous=None` for every class.

## 6. Rollback drill (needs a second package)
1. Make a second package (rerun a stage and package, or package the same build again: it gets a new tag and, for pelias, the same index name, which the deploy handles) and verify it.
2. Deploy it: `flask deploy --config dev-mini --tag <new tag> --inline`. Now `current=<new>`, `previous=r-20261007-1`, nothing older on the targets.
3. `flask deploy-rollback --config dev-mini --inline`: `current=r-20261007-1`, `previous=None`, the new release is removed from the targets (pelias: its index is dropped unless the old release uses the same one), the services run the old data.
4. Optional failure drill: stop Garage and deploy the tiles class: the deploy fails at the upload and nothing changes on the targets.

## 7. Report back
What each check showed, the run numbers, and anything that surprised you. Update `../Docs/MIGRATION-DATA-MANAGER.md` (see below).

## Handover: ticks for `../Docs/MIGRATION-DATA-MANAGER.md`
(That repository is read-only from here; apply these on the developer machine once the drill passed.)
- Package contents and generated parts (tiles `manifest.json`, geodata `manifest.yml`, Placeholder store, snapshot restore names): built.
- Deploy driver `compose-single-machine` (copy, verify, relative `current`/`previous`, only those two kept, rollback), activators for valhalla/geodata/pelias, object-store transport for tiles with `tilesservice-env` activation, orchestrator, `deploy` stage, CLI and the Deploy page: built.
- Still open for other repos: tilesservice reloading on `current.json` (then the `tilesservice-env` activator is unnecessary) and serving styles, glyphs and sprites from the release.
