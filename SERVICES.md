# SERVICES.md — changes required in sibling services

Changes that other SwayRider services (separate repos, not part of this workspace) need when data-manager's build output changes. **Nothing in the sibling repos is changed
from this machine** (CLAUDE.md → Workflow): the changes are implemented on the developers' machines from these documents. The tilesservice and swayrider-api parts below are
derived from their code (tilesservice `642cb8a`, swayrider-api `b1f9ad3`, read on 2026-10-05); the full analysis, file/line references, code sketches, tests and the ordered
list of pull requests are in **`TILESSERVICE-PMTILES.md`**, which is the handover document for that work. Items marked *(verify)* were not checked against code.

> Cross-service ordering, the dev-mini deployment layout and the per-artifact copy/activate procedure are in [`../Docs/MIGRATION-DATA-MANAGER.md`](../Docs/MIGRATION-DATA-MANAGER.md). Deployment is **copy-based** (data-manager may run on another host than the target; artifact classes may sit on different drives), (package/deploy model: [`RELEASE-CONTRACT.md`](RELEASE-CONTRACT.md)), so the target mounts a per-class root (`TILES_ROOT`, `VALHALLA_ROOT`, `PELIAS_ROOT`, `GEODATA_ROOT`) containing `releases/<id>/` and a `current` symlink.

## tilesservice — one Protomaps PMTiles tileset and the map styles

**Why:** data-manager's tiles stage no longer builds tiles. It downloads the Protomaps daily **planet** build (`download-tiles`, ~140 GB, Protomaps basemap schema, Z0–15) and the
release delivers it as `tiles.pmtiles` together with the map styles, glyphs and sprites, instead of three levels of custom-schema MBTiles.
Decisions: 2026-10-01 (PMTiles instead of building tiles), **2026-10-05** (the whole planet is the tileset, no extent extract; `tilesservice` reads the file and serves tiles and styles;
everything stays behind auth; public URLs in styles are filled in at serve time; separate per-user rate limit for the map in the gateway). Not chosen: serving the file statically (the service accepts only gateway service tokens, clients never read the file).
**Decision 2026-10-05 (later the same day): the release lives in an S3-compatible object store (Garage), reversing the earlier "one file on one host needs no object store".** See the object-store contract below.

### Today (code of `tilesservice` 642cb8a)
- Go service; endpoints `GET /v1/tiles/ping` (public), `/v1/tiles/styles`, `/v1/tiles/styles/{name}`, `/v1/tiles/{tileset}/{z}/{x}/{y}`, all but `ping` need a **service token with scope `tiles:serve`**
  (user JWTs are rejected; `swayrider-api` injects its token). Tiles come from `TILES_PATH` as MBTiles in L0/L1/L2 and 10° grid files (`internal/tileindex`, `internal/mbtiles` incl. merging of border tiles);
  `{tileset}` is parsed and ignored; `z > 16` is rejected. Caches: memory LRU + SQLite-backed disk cache keyed by `z/x/y` only. Styles: `STYLES_PATH` (baked into the image), `text/template` with `{{.TilesBaseURL}}`,
  list = `[{"name": …}]`. cgo is needed only for SQLite. Compose (`infra/dev*/layer-20`): bind mounts `${TILES_DATA_PATH}:/data/tiles:ro` and `${TILES_CACHE_PATH}:/data/cache`, `SERVICE_HOST/PORT/PREFIX` = the
  gateway's public URL + `/v1/tiles`, port 34005.

### The new file (checked against the live Protomaps builds, 2026-10-01)
PMTiles **v3**, clustered, gzip-compressed MVT, **Z0–15**, the whole planet; metadata JSON holds `vector_layers`, the header bounds and zoom range. Protomaps basemap schema, layers
`boundaries, buildings, earth, landuse, natural, places, pois, roads, transit, water`; features carry `kind`/`kind_detail`, `min_zoom`, `name` and `name:xx`. Built daily from Protomaps' own OSM snapshot (build date and
schema version are in the file's metadata and in data-manager's asset record).

### Contract: the release in the object store (data-manager writes, `tilesservice` only reads)
> **Update 2026-10-05:** the tiles release is stored in the bucket `swayrider-tiles` and **not** in a bind-mounted directory. The tree below is the **key layout** under `releases/<id>/` (replace `<TILES_ROOT>/releases/<id>/tiles/` by `releases/<id>/`); the relative symlink `current` becomes the object `current.json` (`{"schema":1,"release":"<id>","prefix":"releases/<id>/","updated_at":"…"}`), written last with one PUT and polled by `tilesservice`. Manifest paths stay relative to the release prefix. A local-file backend (directory with `current` symlink, as drawn below) remains for tests and laptops. `tilesservice` gets a read-only key; data-manager's deploy a read/write key (env only). Env names are provisional until the tilesservice PRs: `S3_ENDPOINT`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, source base `s3://swayrider-tiles` or `file:///data/tiles`. Glyphs, sprites and styles are read from the bucket and cached in memory. The mount sentence "mounted, not copied" below applies to the `file` backend only.
```
<TILES_ROOT>/                              bind-mounted read-only into the container
  current -> releases/<id>/tiles           RELATIVE symlink inside the mount (an absolute host path does not resolve in the container)
  releases/<id>/tiles/
    tiles.pmtiles                          the approved planet build; never modified in place
    manifest.json
    styles/<style id>/<version>/light.json    one pair per style; identical layer ids in both variants (smooth setStyle switch)
    styles/<style id>/<version>/dark.json
    glyphs/<fontstack>/<range>.pbf         self-hosted; the font stacks the styles use ("Noto Sans Regular", …)
    sprites/<name>[@2x].{json,png}         one sheet per Protomaps flavor (light, dark, white, black, grayscale)
```
`manifest.json` (schema 1; written by the data-manager release step, read at startup and on reload):
```json
{"schema": 1, "release": "r-20261005-1",
 "tileset": {"name": "planet", "build": "20261004", "date": "2026-10-04", "schema_version": "4.15.2", "file": "tiles.pmtiles"},
 "styles": [{"id": "classic", "label": "Classic", "version": "3", "default": true,
             "variants": {"light": "styles/classic/3/light.json", "dark": "styles/classic/3/dark.json"}}]}
```
Style files are **Go templates** rendered by `tilesservice` per request (the mechanism exists today): `"tiles": ["{{.TilesBaseURL}}/{{.Tileset}}/{z}/{x}/{y}"]`, `"glyphs": "{{.TilesBaseURL}}/fonts/{fontstack}/{range}.pbf"`,
`"sprite": "{{.TilesBaseURL}}/sprites/<flavor>"`, source `"maxzoom": 15`. `TilesBaseURL` = `SERVICE_HOST[:SERVICE_PORT]SERVICE_PREFIX` (the **gateway's** public URL + `/v1/tiles`); `Tileset` = the tileset name from the manifest.
MapLibre's `{z}/{x}/{y}` use single braces and do not collide with `{{ }}`. The release directory is **mounted**, not copied (140 GB per release; the data root and the tiles disk differ); the previous release stays for rollback.

### Public API (all under `/v1/tiles/`, all behind the gateway's auth and `tiles:serve`)
| Endpoint | Behaviour |
|---|---|
| `GET /v1/tiles/{tileset}/{z}/{x}/{y}` | `tileset` is meaningful: `base` = legacy MBTiles (kept during the migration), `planet` = PMTiles. Gzip tiles pass through; z/x/y validated; 204 outside the file's zoom range or for missing tiles |
| `GET /v1/tiles/{tileset}/tiles.json` | TileJSON from the PMTiles header/metadata (`bounds, center, minzoom, maxzoom, attribution` incl. "© OpenStreetMap contributors", `vector_layers`, `tiles`) |
| `GET /v1/tiles/styles` | backwards compatible list `[{name, id, label, version, variants}]`; legacy names `light`/`dark` stay as aliases of the default style |
| `GET /v1/tiles/styles/{name}`, `/styles/{id}/{variant}`, `/styles/{id}/{version}/{variant}` | rendered style JSON (templates filled in); versioned URLs immutable, unversioned short-lived, `ETag` |
| `GET /v1/tiles/fonts/{fontstack}/{range}.pbf`, `/sprites/{name}[@2x].{json,png}` | static assets from the release directory, long cache |
| `GET /v1/tiles/ping` (liveness, public upstream) and a readiness endpoint | readiness reports tileset build, schema version, release and style versions |

Client flow: list styles -> user picks a style `id` -> the app takes the `light` or `dark` variant of the system theme and switches when the theme changes. Style versions stay reachable while apps use them.

### What `tilesservice` has to change (summary; details, code sketches and tests in `TILESSERVICE-PMTILES.md` §3–§6)
1. **PR 0, independent:** fix the gzip pass-through that ignores `Accept-Encoding` (`http_tile.go:143-154`) and the test that locks it in.
2. PMTiles reader (library `go-pmtiles` or ~300 own lines), tile endpoint with header-driven zoom limits, `tileset` selection, ETag.
3. Release holder with atomic reload (SIGHUP/polling the `current` symlink), readiness, HTTP server timeouts (review 2026-08-19 #1).
4. Styles: manifest-driven list/variants/versions, template variables `TilesBaseURL` and `Tileset`, ETag/Cache-Control. 5. Glyph and sprite handlers.
6. Remove the tile caches (they key on `z/x/y` without a tileset id and are pointless for pre-gzipped tiles), MBTiles/grid code after the migration, SQLite and cgo; keep accepting `COMPRESSION_*`/`DISK_CACHE_*` for one release.
Compose: mount the directory that holds `current`; env `TILES_ROOT`; drop the cache volume. Migration: legacy `base` and the new tileset side by side (the `{tileset}` segment), clients switch, then the legacy code goes.

### Client (MapLibre GL JS / Native)
- All map requests (style, tiles, glyphs, sprites) need the user's JWT: web with the `access_token` cookie, native with an `Authorization` header on every request below the tiles base URL (`transformRequest`), refreshed before the 15-minute expiry.
- Style list -> pick -> light/dark by system theme. Remove overrides that referenced the old layers; layer/attribute mapping: `places` -> `places` (`kind`, `kind_detail`, `min_zoom`, `name:xx`), `roads`/`highway_labels` -> `roads` (`kind`, `kind_detail`, `ref`, `network`),
  `water` -> `water`, `forest`/`urban` -> `landuse`, `natural`, `earth`; `boundaries`, `pois`, `transit`, `buildings` are new. One source with `maxzoom: 15`, the client over-zooms.
- Lost compared with the legacy tiles (Protomaps tiles are not ours to change): yellow motorway ramps, A/E/N shield data beyond `ref`/`network`, the legacy `population` string, 26-language country labels (use `name:xx`), the forest/urban filtering.
  A custom layer would mean building tiles ourselves again, the fallback if the schema ever blocks a requirement.

## swayrider-api — the gateway in front of `tilesservice` (code of `b1f9ad3`)
It reverse-proxies the whole `/v1/tiles/` prefix (`internal/server/routes.go:57`, `internal/handlers/tiles.go`) behind `RequireVerifiedUser` with its own service token (scopes include `tiles:serve`), so **new `tilesservice` endpoints need no route** and headers
(`ETag`, `Cache-Control`, `Content-Encoding`, `If-None-Match`) pass through. Required changes (details in `TILESSERVICE-PMTILES.md` §8):
1. **Rate limit class `tiles`, per user** (`internal/middleware/ratelimit.go`, `config.go`, `server.go`): today `/v1/tiles/*` is class `public`, 600 requests/min **per IP** shared with `/health` and public keys. New `RATE_LIMIT_USER_TILES`, default 3000/min per user (estimate; set from measured traffic).
2. `MaxIdleConnsPerHost` of the proxy transport (10) raised for map bursts (e.g. 50–100) in `internal/handlers/proxy.go`.
3. Documentation: README line "No authentication required" for tiles is wrong (verified user is required); `API.md` and `api/openapi.yaml` list the new sub-paths and the new rate-limit class.
4. Check that the service-token refresh fix of `review/CODE_REVIEW_2026-08.md` is deployed (a stale token once caused weeks of tile failures).

### Open questions
- Tileset name (`planet`?) and whether the build id is part of it; go-pmtiles library or own reader (spike); the real `RATE_LIMIT_USER_TILES` after measuring; MapLibre version honours `source.maxzoom: 15` (standard); Protomaps' terms for repeated automated downloads and public serving;
  disk for two planets (current + previous) where the release directory lives.

## Other services
- **routerservice / Valhalla, searchservice / Pelias:** unaffected by the tile schema change.
- **Pelias (planned, see DESIGN.md → "Locality boundaries"):** the boundary/placeholder patch is a separate change list, tracked there.
- **Pelias services (deploy contract, see DESIGN.md → "Pelias stage"):** per region the `pelias` stage delivers `pelias-index-snapshot` (restored into the live Elasticsearch, alias switched), `pelias-config` (the production `pelias.json`) and `pelias-wof` (the WOF `sqlite/` directory the PIP service reads, patched databases included); `pelias-interpolation` delivers `street.db` and `address.db` for an interpolation service (`./interpolate server address.db street.db`, port 4300) that does not exist in `infra` yet; the API's `pelias.json` needs `interpolation.client = {adapter: http, host: http://pelias-interpolation:4300}` when it does. The placeholder store stays the approved `placeholder:store` download.
- **regionservice:** consumes region definitions; no tile-schema dependency expected *(verify)*.
