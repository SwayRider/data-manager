# data-manager

Flask application that builds the SwayRider map data (OSM → borders → Valhalla → Pelias, plus tiles and styles) as
orchestrated, database-tracked stages, packs the result into a tagged **package**, and **deploys** a package to an
environment (first target: `infra/dev-mini` on the same machine). It replaces the scripts of `data-pipeline`.

> **Status: debug build only.** There is no release build, no container image and no installer yet. data-manager runs
> from a checkout with `./debug.sh` (Flask development server with the debugger, plus a worker), as the user that owns the
> data. The development server has **no authentication** and listens on all interfaces (`0.0.0.0:5050`): run it on a
> trusted network only. Tags of this repository mark states of the code, not deployable artifacts.

## Prerequisites

**On the machine**
- Linux with Python 3.13 (3.12+ is likely fine; CI and development use 3.13), `git`, `docker` (your user can run it).
- The system tools the stages call. Settings → Tools in the UI detects them and shows the Debian install command for
  each; the required ones are `osmium-tool`, `gdal-bin` (`ogr2ogr`), `rsync`, `openssh-client`, `git`, `cmake`, `make`,
  `g++`, Node.js ≥ 22, `npm` and libpostal (a C library with its model data; built from source, see Settings → Tools).
  Optional: `curl`, `unzip`, `zip`, `tar`, `overturemaps`, `aws`, `pmtiles`. Valhalla and the Pelias importers are cloned
  and compiled by data-manager itself (Settings → Tools → Build, into `DATA_ROOT/tools`).
- Redis for the job queue: the dedicated one of `infra/data-manager` (`docker compose up -d redis`, port 36389). Not the
  shared `sw-dev-redis`.
- Plenty of disk: a full build of the Benelux/France/Germany region set takes several hundred GB for downloads and work
  files, a package is ~360 GB (it includes the 140 GB planet tiles), and a deploy target holds the current and the
  previous release of each class. Put `PACKAGE_ROOT` on a big disk.

**Shared access (once per machine)** — `scripts/init-host.sh`
The package repository and the deploy targets are shared between administrators through a Unix group (`swdata`), not
owned by a single user: the directories belong to `root:swdata`, are setgid and carry a default ACL, so what one
administrator creates stays writable for the others.
- `scripts/init-host.sh` creates the group if it is missing, adds you to it and continues inside `sg` so the group is
  active, checks the `acl` tools and docker access, and prepares `PACKAGE_ROOT`. It only shows what it would do; run it
  with `--apply` and choose **A** (run it with sudo), **M** (do it yourself, it checks again) or **Q**.
- The deploy targets are prepared by `infra/dev-mini/scripts/prepare-host.sh` (same flow, `--dry-run` is the default).
- Another administrator is added with `sudo usermod -aG swdata <name>`. They must log in again or run `newgrp swdata`
  **before** starting `./debug.sh`, otherwise data-manager cannot write to the shared directories.
- `DATA_ROOT` (database, downloads, library, work files) stays private to the user that runs data-manager.

## Setup

```
git clone git@github.com:SwayRider/data-manager.git && cd data-manager
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then edit it, see below
scripts/init-host.sh            # read the plan; --apply to carry it out
(cd ../infra/data-manager && docker compose up -d redis)
./migrate.sh                    # create/upgrade the database (alembic upgrade head)
flask seed-countries            # once: the country catalog (all Natural Earth countries)
```

`.env` is bootstrap configuration only (`.env.example` explains every line): `REDIS_URL`, `DATABASE_PATH`, `DATA_ROOT`,
`PACKAGE_ROOT` (empty = `DATA_ROOT/releases`; set it to the shared package repository), `SECRET_KEY`, and for deploys to
the tiles object store `DM_S3_ACCESS_KEY` / `DM_S3_SECRET_KEY` (the read/write key of Garage). Everything else —
regions, profiles, tool versions, public URLs — is configured in the UI and stored in the database.

## Run

```
./debug.sh                      # UI on http://<host>:5050, plus an RQ worker (stopped when the server stops)
NO_WORKER=1 ./debug.sh          # only the server, when a worker already runs elsewhere
```

**Restart `./debug.sh` after every code change to stage code** — the worker does not reload — and after changing `.env`
(web app and worker both read it when they start). Run `./migrate.sh` after pulling a change that adds a migration.

## Workflow in short

1. **Configure** (UI): regions (map), profiles, style, transit feeds; Settings → Tools: detect/build the tools.
2. **Build** (UI or `flask` CLI): download and process stages in order; a clean run waits for your approval, and only
   approved runs feed later stages.
3. **Package** (Build → Package): a tagged, self-describing copy in the package repository; verify it; then clean up the
   intermediate files. The **Repo** page lists packages, labels, protection and verification. Repository retention is
   manual.
4. **Deploy** (`/deploy` or `flask deploy-plan` / `flask deploy`): copy a verified package to an environment, check it,
   switch `current`, restart/health-check the services, keep only `current` and `previous`; `flask deploy-rollback` goes
   back. The first real deploy, class by class, is the runbook `DEPLOY-DEV-MINI.md`.

## Tests

```
pytest                           # the full suite (needs no Redis for most tests; the RQ tests need the dedicated Redis)
```

## Documents

| File | What it is |
|------|------------|
| `DESIGN.md` | The authoritative plan: architecture, schema, stages, roadmap, risks. Read before adding features. |
| `CLAUDE.md` | Orientation for development (also for Claude Code): workflow, commands, architecture notes. |
| `RELEASE-CONTRACT.md` | Package layout, deploy driver contract and the as-built notes of the deploy. |
| `DEPLOY-DEV-MINI.md` | Runbook for deploying a package to the dev-mini stack. |
| `SERVICES.md`, `TILESSERVICE-PMTILES.md` | What the services expect from the data (contracts, handover notes). |
| `LAYOUT.md` | Generic UI layout sketches. |
| `../Docs/MIGRATION-DATA-MANAGER.md` | The platform-level migration plan (services, infra, deployment strategy). |

Contributions: topic branch, signed commits (`git commit -s`, the DCO check runs in CI) and a pull request; never on `main`.
