# TILESSERVICE-PMTILES.md — analysis of the changes needed in `tilesservice`

Analysis of `../tilesservice` (checked out at `642cb8a`, Go 1.26, ~6,200 lines including tests) against the data-manager decision of 2026-10-05
(DESIGN.md → "Decision 2026-10-05", SERVICES.md): the whole Protomaps planet build as one `tiles.pmtiles`, styles served as named
light/dark pairs, public URLs filled in at serve time. Written from the code, the Dockerfile and `infra/dev*/layer-20/compose.yml` of `tilesservice`, and from `../swayrider-api` (checked out at `b1f9ad3`), the gateway that is
the only caller (section 8). Nothing here has been implemented or run.

## 1. Conclusions first

1. **Option (b), serving the file statically, is not possible.** `tilesservice` accepts only service tokens with scope `tiles:serve`
   (`cmd/tilesservice/main.go:128-158`); user JWTs get 403, and clients reach it through `swayrider-api`, which injects its own token. Tiles, styles,
   glyphs and sprites must therefore keep flowing through `tilesservice` (option (a)); clients never read the PMTiles file.
2. **The service is smaller to change than to keep.** The MBTiles-specific half (`internal/mbtiles` incl. the tile merge, `internal/tileindex`, the
   10° grid and L0/L1/L2 logic) is replaced by a single-file reader, and the two-tier tile cache (`internal/tilecache`, ~900 lines + ~1,700 lines of tests,
   the only reason for cgo/SQLite besides MBTiles) is no longer worth its cost for pre-gzipped tiles read from one local file.
3. **Styles are already templated at serve time**, exactly the mechanism wanted (`{{.TilesBaseURL}}` in `http_style.go`). It only has to grow
   (more variables, named style sets with a manifest, versions, caching headers); it does not have to be invented.
4. **Four existing behaviours would break or go stale with PMTiles** and need explicit changes: the zoom limit of 16, the cache key without a tileset id
   (stale tiles after a release switch), the gzip pass-through that ignores `Accept-Encoding`, and the glyph URL (`fonts.openmaptiles.org`, font stack
   "Open Sans") that does not match Protomaps styles ("Noto Sans …").
5. **`swayrider-api` needs no routing change**: it proxies the whole `/v1/tiles/` prefix with a plain reverse proxy, so new endpoints, headers (`ETag`, `Cache-Control`,
   `Content-Encoding`) and `If-None-Match` pass through. **Decided (2026-10-05): everything stays behind auth** (tiles, styles, glyphs, sprites; a verified user's JWT, cookie or header)
   and the map gets **its own, generous per-user rate limit** instead of the shared per-IP 600/minute (section 8.3).
6. **The `{tileset}` path segment, parsed and ignored today, is the migration lever**: serve the legacy MBTiles as tileset `base` and the PMTiles planet as a
   new tileset next to it, so old and new clients run side by side with no `/v2` prefix.

## 2. How the service works today

| Area | What the code does |
|---|---|
| Endpoints | `GET /v1/tiles/ping` (public), `GET /v1/tiles/styles`, `GET /v1/tiles/styles/{name}`, `GET /v1/tiles/{tileset}/{z}/{x}/{y}` (all need `tiles:serve`) |
| Tiles | `tileindex.GetTile` maps zoom to L0 (z≤6, one file), L1 (z7–10) or L2 (z11–16) and the tile's corners to a 10° grid file `N50_E000.mbtiles`; a tile on a cell border is read from up to 4 files and merged (`mbtiles.MergeTiles`, uses `paulmach/orb`). Readers are cached per path for the life of the process. |
| Limits | `z > 16` → 400; x/y range checked; unknown tile → 204; `tileset` ignored (`http_tile.go:75`) |
| Response | `Content-Type: application/vnd.mapbox-vector-tile`, `Cache-Control: public, max-age=86400`, `Vary: Accept-Encoding`; already-gzipped data is sent as is with `Content-Encoding: gzip`, otherwise gzip-compressed (BestSpeed) when the client accepts it, through a memory LRU and an optional disk cache |
| Caches | `CompressedTileCache` (memory LRU, key `"z/x/y"`), `DiskTileCache` (SQLite metadata + files, key `z/x/y`, cleared at startup only), `TwoTierCache` |
| Styles | `STYLES_PATH` directory with `<name>.json`; parsed with `text/template`, cached by file mtime, rendered with `{{.TilesBaseURL}}`; the list returns `[{"name": …}]` for the files on disk; names validated by `^[a-zA-Z0-9_-]+$` |
| Style content (`assets/map/styles/{light,dark}.json`) | 82 layers each, source `swayrider` with `tiles: ["{{.TilesBaseURL}}/base/{z}/{x}/{y}"]`, `maxzoom 16`, glyphs `https://fonts.openmaptiles.org/{fontstack}/{range}.pbf` (fonts "Open Sans Regular/Bold"), no sprite |
| Config | `TILES_PATH`, `STYLES_PATH`, `COMPRESSION_*`, `DISK_CACHE_*`, `SERVICE_HOST/PORT/PREFIX` (-> `TilesBaseURL`), `AUTHSERVICE_*`, JWT key refresh |
| Auth/CORS | `requireTilesAuth` middleware (JWT keys from authservice, refreshed every 300 s); CORS `*` set twice (`rs/cors` and in each handler) |
| Image | Multi-stage Dockerfile, **cgo on** (go-sqlite3), styles **baked into the image** (`COPY assets/map/styles`, `ENV STYLES_PATH`), healthcheck on `/ping` |
| Deployment (`infra/dev*/layer-20/compose.yml`) | bind mounts `${TILES_DATA_PATH}:/data/tiles:ro` and `${TILES_CACHE_PATH}:/data/cache`; `SERVICE_HOST/PORT/PREFIX` from `TILESSERVICE_PUBLIC_*` (the public URL is the API host with prefix `/v1/tiles`, i.e. the `swayrider-api` proxy); published on 34005 |

Not present: server HTTP timeouts (review of 2026-08-19, finding 1, still open), a readiness check that looks at the tiles, any ETag/conditional request,
glyph or sprite serving, any notion of a tileset version.

## 3. Findings that change earlier assumptions

### 3.1 Auth model: everything goes through `swayrider-api`
- Clients never talk to `tilesservice`; `swayrider-api` proxies with a service token. The public URLs in a style (`TilesBaseURL`) are the API's URLs. The gateway proxies the
  whole `/v1/tiles/` prefix (`internal/server/routes.go:57`), so **new endpoints need no gateway route**; details and side effects in section 8.
- **Glyphs and sprites are the awkward part.** MapLibre fetches them with its own requests; today they come from a public host without auth. If they move
  under the authenticated prefix, the apps must also attach credentials to those requests (`transformRequest` or equivalent) like they presumably do for tiles.
  **Decided: authenticated like tiles** (section 7, 8.2); every app attaches the same credentials to style, glyph and sprite requests.

### 3.2 Zoom
- `http_tile.go:95` rejects `z > 16` with 400; the file is Z0–15 and the style source says `maxzoom: 16`. New style sources get `maxzoom: 15` (clients then over-zoom and never ask
  for z16). The server should read `minzoom/maxzoom` from the PMTiles header, answer 204 for tiles outside that range and keep a sane upper bound (e.g. 22) against abuse,
  instead of a hard-coded 16.

### 3.3 Cache key and cache lifetime
- Both caches key on `"z/x/y"` only. After a tileset replacement a memory or disk cache would keep serving **old** tiles (the disk cache is cleared only at process start).
  With pre-gzipped tiles read straight from one file (page cache + directory cache do the work), the compression cache has nothing to save. Recommendation: **remove both
  caches**; if a hot-tile LRU is wanted later, key it by `(tileset build id, z, x, y)`.
- `Cache-Control: public, max-age=86400` on a mutable URL means clients and proxies may show up to a day of old tiles after a new tileset. Accepted for the first step;
  a CDN-friendly variant puts the build id into the URL (section 5.4).

### 3.4 Compression (fix described in 5.5)
- The "already gzipped" branch (`http_tile.go:144-154`) always sends `Content-Encoding: gzip`, **even when the client did not send `Accept-Encoding: gzip`**. Today it rarely
  matters; with PMTiles every tile is stored gzipped, so every response takes that branch. Fix: gunzip for clients that cannot decode (or require gzip support). Through `swayrider-api` the bug is masked: the Go reverse-proxy transport adds `Accept-Encoding: gzip`
  itself when the client sent none and transparently decompresses, so such a client gets plain bytes. It stays a bug for direct callers, tests and any other proxy. Also read the header's
  `tile_compression` (Protomaps builds are gzip; the reader must refuse or handle other values) and `tile_type` (must be MVT).

### 3.5 Fonts and sprites
- The legacy style uses fonts `Open Sans Regular/Bold` from a third-party host. The Protomaps styles produced by data-manager (`@protomaps/basemaps`) use Noto Sans stacks
  (`protomaps/basemaps-assets`). The service needs a glyph endpoint with exactly the stacks the styles reference, and sprite sheets for the Protomaps flavors. Both are
  vendored on the data-manager side already (`blueprints/configure/static/map-assets/`); only the delivery is missing.

### 3.6 Packaging
- Without MBTiles and the disk cache there is no SQLite left: **cgo can be dropped** (static binary, simpler cross-build, smaller image). `paulmach/orb` goes with the tile merge.
  Check with `go mod why` after the removal; the JWT/authservice dependencies stay.
- Styles are baked into the image today (`COPY assets/map/styles`), so a style change currently needs a new image. Moving them to the release directory (section 4) is the point of
  the data-manager `styles` stage; keep the baked-in files only as a fallback or remove them.

## 4. Target design

### 4.1 Data on disk (written by data-manager, read-only for the service)
```
<TILES_ROOT>/                              bind-mounted read-only; TILES_ROOT = the directory that contains the release pointer
  current -> releases/<id>/tiles           relative symlink INSIDE the mount (resolves in the container; an absolute host path would not)
  releases/<id>/tiles/
    tiles.pmtiles
    manifest.json
    styles/<id>/<version>/{light,dark}.json
    glyphs/<fontstack>/<range>.pbf
    sprites/<name>[@2x].{json,png}
```
`manifest.json` (schema 1) has exactly the shape given in SERVICES.md → "Contract" (release id, `tileset {name, build, date, schema_version, file}`, `styles [{id, label, version, default, variants{light, dark}}]`). The service reads the
manifest at startup and on reload; it never scans the directory. Relative symlinks inside one mounted directory do work in a container, so the release pointer can stay a symlink.

### 4.2 Public API (all under the existing prefix, all behind `tiles:serve`, i.e. proxied by `swayrider-api`)
| Endpoint | Change |
|---|---|
| `GET /v1/tiles/{tileset}/{z}/{x}/{y}` | `tileset` becomes meaningful: `base` = legacy MBTiles (kept during migration), `planet` (name to decide) = PMTiles |
| `GET /v1/tiles/{tileset}/tiles.json` | **new**: TileJSON from the PMTiles header/metadata (`bounds, center, minzoom, maxzoom, attribution, vector_layers`, `tiles` URL template). (Go's `ServeMux` has no `{tileset}.json` wildcard: see 4.5.) |
| `GET /v1/tiles/styles` | extended, backwards compatible: `[{name, id, label, version, variants}]` (existing clients read only `name`); legacy names `light`/`dark` stay as aliases of the default style |
| `GET /v1/tiles/styles/{name}` | unchanged for `light`/`dark`; plus `GET /v1/tiles/styles/{id}/{variant}` (newest version) and `GET /v1/tiles/styles/{id}/{version}/{variant}` (fixed version) |
| `GET /v1/tiles/fonts/{fontstack}/{file}` | **new**; `file` = `<range>.pbf` (`fontstack` may contain spaces and commas; validate against the directory, never join raw user input into a path) |
| `GET /v1/tiles/sprites/{file}` | **new**; `file` = `<name>[@2x].{json,png}` |
| `GET /v1/tiles/ping` | stays public and cheap (liveness); add `GET /v1/tiles/ready` (or fields in ping) that reports tileset build, schema version, manifest release and style versions |

### 4.3 Style templating (extends `http_style.go`)
Template variables today: `TilesBaseURL` (= `SERVICE_HOST[:SERVICE_PORT]SERVICE_PREFIX`). Add **one** variable, `Tileset` (the tileset name from the manifest); glyphs and sprites live under the same base, so no `GlyphsURL`/`SpriteURL` is needed.
data-manager's `styles` stage emits a style whose source is
```json
"sources": {"protomaps": {"type": "vector", "tiles": ["{{.TilesBaseURL}}/{{.Tileset}}/{z}/{x}/{y}"], "minzoom": 0, "maxzoom": 15,
                          "attribution": "<a href=\"https://github.com/protomaps/basemaps\">Protomaps</a> © <a href=\"https://openstreetmap.org\">OpenStreetMap</a>"}},
"glyphs": "{{.TilesBaseURL}}/fonts/{fontstack}/{range}.pbf",
"sprite": "{{.TilesBaseURL}}/sprites/<flavor>"
```
(`<flavor>` = `light`, `dark`, `white`, `black`, `grayscale`; MapLibre appends `.json`, `.png` and `@2x`.) An explicit `tiles` array with `maxzoom` avoids a second authenticated request for `tiles.json` and keeps the style self-contained; the TileJSON endpoint
stays for tools. MapLibre's `{z}/{x}/{y}` and `{fontstack}/{range}` use single braces and do not collide with `{{ }}`. Keep the mtime cache but add an `ETag` (content hash of the rendered output) and `Cache-Control`: versioned URLs
immutable, unversioned (`/{id}/{variant}`) short-lived. Templates are parsed with `text/template`: a style that fails to parse is a 500 for that style only and must make `ready` report it at load time instead of at the first request.

### 4.4 Reload
A release switch must not drop requests: open the new `tiles.pmtiles` and manifest, swap an atomic pointer, close the old reader after in-flight requests finish (reference count or `RWMutex` around the swap).
Trigger: `SIGHUP` and/or polling the `current` symlink target every few seconds (cheap, works inside the container). A failed open keeps the old tileset and makes `ready` report it.

### 4.5 Go `ServeMux` specifics for the new routes
The service already uses Go 1.22+ method/wildcard patterns (`main.go:345-362`) and parses the rest of the path by hand in the handlers. Wildcards must be whole path segments (`{x}`, not `{x}.json` or `{range}.pbf`), so register `{file}` and strip/validate the suffix in the handler.
Patterns that overlap resolve by specificity: `GET /v1/tiles/styles/{id}/{version}/{variant}` (6 segments) is a subset of `GET /v1/tiles/{tileset}/{z}/{x}/{y}` and wins for `styles/…`; literals (`ping`, `ready`, `styles`, `fonts`, `sprites`) beat `{tileset}`. Add a routing test that asserts which handler
answers `/v1/tiles/styles/classic/3/light`, `/v1/tiles/planet/tiles.json`, `/v1/tiles/fonts/Noto%20Sans%20Regular/0-255.pbf` and `/v1/tiles/planet/0/0/0`, since a precedence conflict panics at startup and a wrong winner is silent. Do not use a tileset or style
named `styles`, `fonts`, `sprites`, `ping` or `ready`.

## 5. Change list

### 5.1 Remove
| Path | Size | Why |
|---|---|---|
| `internal/mbtiles/{reader,merge}.go` + tests | ~230 + ~670 lines | MBTiles gone (kept only while tileset `base` still exists, section 6) |
| `internal/tileindex/index.go` + test | ~320 + ~510 lines | grid/zoom-to-file mapping |
| `internal/tilecache/*` + tests | ~900 + ~1,700 lines | see 3.3; also removes go-sqlite3/cgo |
| env/flags `COMPRESSION_*`, `DISK_CACHE_*` | | keep accepted-and-ignored for one release (the compose files pass them) and log a deprecation warning |
| `CACHE_IMPLEMENTATION.md` | | obsolete |

### 5.2 Replace / add (code)
| Component | Work | Size |
|---|---|---|
| **PMTiles reader** (`internal/pmtiles`) | Open file, parse the 127-byte v3 header, read root + leaf directories (gzip, varint-delta entries, run-lengths), look up a tile by Hilbert tile id, read the byte range. Either use `github.com/protomaps/go-pmtiles` (BSD-3; check its dependency weight, it brings object-storage code: `go mod graph` first, and verify the exact API of the version pinned) or write ~300 lines against the published spec (data-manager already decodes header/metadata in `services/pmtiles.py`). Add a directory cache and Range reads on an `os.File` (`ReadAt`). **Recommendation: a half-day spike of both, then decide.** | M |
| `http_tile.go` | Use the reader; header-driven zoom limits (3.2); pass-through of gzip with the `Accept-Encoding` fix (3.4); remove cache calls; ETag (tileset build + coordinates) and `Cache-Control`; use the `tileset` segment | M |
| `http_style.go` | Manifest-driven list/variants/versions (4.2), new template variables, ETag/Cache-Control (4.3), keep `validStyleName` for ids and add a version pattern (`^[0-9A-Za-z._-]+$`, reject `.`/`..` segments); stop scanning the directory | M |
| new `http_assets.go` | Glyph and sprite handlers with strict path validation and long cache headers | S |
| new `tileset` holder | Atomic current tileset (reader + manifest + styles dir), `Reload()`, SIGHUP/poll (4.4) | M |
| `cmd/tilesservice/main.go` | New config: `TILES_ROOT` (directory with `current`), keep `TILES_PATH` for legacy `base`; remove cache wiring; `ReadHeaderTimeout/ReadTimeout/WriteTimeout/IdleTimeout` on `http.Server` (review 2026-08-19 #1); readiness | S–M |
| `Dockerfile` | Drop gcc/cgo, build static, drop or keep the styles `COPY` as fallback; healthcheck stays on `/ping` | S |
| `go.mod` | Remove `go-sqlite3`, `paulmach/orb` (verify nothing else needs them); possibly add go-pmtiles | S |

### 5.3 Tests
The service has solid tests for what is removed (`mbtiles`, `tileindex`, `tilecache`: ~2,900 lines) and for handlers (`http_tile_test.go` 378, `http_style_test.go` 294, `main_test.go` 283).
Needed: a small PMTiles fixture generator (or a committed tiny `.pmtiles`), reader tests (header, directories, run-lengths, leaf directories, missing tile, wrong compression/type), tile-handler tests
(zoom bounds from header, 204, gzip vs non-gzip client, ETag/304), style tests (manifest, variants, versions, template variables, traversal attempts), glyph/sprite traversal tests, reload test
(swap under concurrent requests with `-race`), and an auth regression test that every new route sits behind `requireTilesAuth`.

### 5.4 Optional: immutable tile URLs
With everything behind auth (8.2) a CDN in front of the gateway is not planned, so this only matters for long *client* caches. If wanted, put the build id in the tile URL (`/v1/tiles/planet-<build>/{z}/{x}/{y}`, `Cache-Control: immutable`) and let the style template fill it (`{{.Tileset}}`). Cost: a client with a cached
style keeps asking for the old build, so the previous tileset must stay loadable for a while (two readers, retention). Not needed for the first step; the stable URL with ETag and a 24 h `max-age` (as today) is enough.

### 5.5 Fix: gzip pass-through that ignores `Accept-Encoding`
**Where:** `internal/server/http_tile.go:143-154`.
```go
if compression.IsGzipped(tileData) {
    w.Header().Set("Content-Encoding", "gzip")   // sent whatever the client accepts
    ...
    w.Write(tileData)
    return
}
```
**Problem.** A tile that is stored gzipped is always sent as gzip, also to a client that did not offer `Accept-Encoding: gzip` (or sent `gzip;q=0`). That breaks HTTP semantics and `Vary: Accept-Encoding`
(the header is set, but the representation does not follow it). Today it hardly shows, because the gateway's transport decompresses for clients without the header (3.4) and the legacy MBTiles are partly stored uncompressed;
with PMTiles **every** tile is gzipped, so every response takes this branch. It is also an order-of-checks problem: the gzip test comes before the `SupportsGzip` test below it, which therefore only ever
applies to uncompressed data.

**Fix.** Decide per request from two facts, what the stored bytes are and what the client accepts, and make the handler's last step one function:

| Stored tile | Client accepts gzip | Response |
|---|---|---|
| gzip | yes | stored bytes as is, `Content-Encoding: gzip` (no CPU) |
| gzip | no | gunzip, no `Content-Encoding` |
| identity (legacy MBTiles only) | yes | gzip on demand (as today, through the cache while it exists) |
| identity | no | stored bytes as is |

```go
// writeTile sends one tile in the representation the client accepts.
func (h *TileHTTPHandler) writeTile(w http.ResponseWriter, r *http.Request, data []byte, storedGzip bool) {
    hdr := w.Header()
    hdr.Set("Content-Type", ContentTypeMVT)
    hdr.Set("Cache-Control", "public, max-age=86400")
    hdr.Set("Vary", "Accept-Encoding")

    wantGzip := acceptsGzip(r)
    switch {
    case storedGzip && !wantGzip:
        plain, err := gunzip(data)            // bounded read, e.g. io.LimitReader(.., 16<<20)
        if err != nil { http.Error(w, "Failed to decode tile", http.StatusInternalServerError); return }
        data = plain
    case !storedGzip && wantGzip:
        data = compressOrFallback(data)       // existing CompressGzip(BestSpeed) path
        hdr.Set("Content-Encoding", "gzip")
    case storedGzip && wantGzip:
        hdr.Set("Content-Encoding", "gzip")
    }
    hdr.Set("Content-Length", strconv.Itoa(len(data)))
    w.WriteHeader(http.StatusOK)
    _, _ = w.Write(data)
}
```
- **`acceptsGzip`:** parse `Accept-Encoding` properly: a `gzip` (or `*`) token that is not `;q=0`. `compression.SupportsGzip` from `swlib` is used today; its source is not in the module cache here, so it was not checked.
  If it only does a substring match, replace it by a small local helper (a `gzip;q=0` request would otherwise be treated as "accepts").
- **`storedGzip`:** with PMTiles take it from the header's `tile_compression` (1 = none, 2 = gzip; refuse anything else at open, 3.4), not from the magic bytes. For the legacy MBTiles keep `compression.IsGzipped(data)`.
- **Validators:** if an `ETag` is added later (4.3/5.2), the gzip and identity representations need different values (or a weak ETag), `Vary` is already in place.
- **Cost:** gunzip of a 50–500 KB tile is sub-millisecond; clients without gzip are rare (the gateway forwards `Accept-Encoding`), so no cache is added for the decompressed form.

**Tests** (`internal/server/http_tile_test.go`):
- **Change** `TestTileHandler_ServesPreCompressed` (lines 288–311): it currently requests **without** `Accept-Encoding` and asserts `Content-Encoding: gzip`, i.e. it locks in the bug. Make it send `Accept-Encoding: gzip` and keep the assertions.
- **Add** gzip tile + no `Accept-Encoding` -> 200, no `Content-Encoding`, body equals the plain bytes; gzip tile + `Accept-Encoding: gzip;q=0` -> same; gzip tile + `Accept-Encoding: br, gzip;q=0.8` -> gzip pass-through;
  corrupt gzip data -> 500; `Vary: Accept-Encoding` and a correct `Content-Length` in every case.
- Keep `TestTileHandler_ServesUncompressed` and `TestTileHandler_CompressesOnDemand` for the legacy path.

**Scope and timing.** The fix is independent of PMTiles: it can ship first as a small PR 0 (one function, ~40 lines, three tests), is safe for old and new clients (those that accept gzip see no change), and removes a latent
bug before the PMTiles reader makes every response depend on it. Verify afterwards through the gateway with `curl --compressed` and with `curl -H 'Accept-Encoding: identity'` against a tile path.

## 6. Migration without a big bang
1. Deploy a `tilesservice` that serves **both**: `base` (MBTiles, unchanged) and the new PMTiles tileset, plus the new style/glyph/sprite endpoints. Old clients are unaffected.
2. data-manager `styles` stage and deploy deliver the release directory; apps adopt the style list, pick a style id and switch light/dark by system theme.
3. When all clients use the new tileset: remove `base`, `internal/mbtiles`, `internal/tileindex` and the legacy styles, drop cgo.
This is also the order that keeps the work reviewable: PR 0 (the gzip fix of 5.5, independent), PRs 1 (reader + tile endpoint), 2 (tileset holder + reload + readiness + timeouts), 3 (styles manifest/variants/template variables), 4 (glyphs/sprites), 5 (remove caches and legacy, packaging).

## 7. Decisions needed and open questions
1. ~~Glyphs/sprites: authenticated or public?~~ **Decided: authenticated, like everything else under `/v1/tiles/`.** Consequence: every app must attach credentials to glyph and sprite requests (section 8.2), and there is no CDN in front of the map for now.
2. **Tileset name** for the PMTiles planet (`planet`? `world`?) and whether the build id is part of it (5.4).
3. **go-pmtiles library or own reader** (spike).
4. **How the apps pick up a new release**: style version in the list is enough if apps re-read the list on start; define the cache lifetime of `GET /styles`.
5. ~~`swayrider-api` rate limit for map traffic~~ **Decided: a separate `tiles` class, per user, with a generous limit** (section 8.3). Open: the actual number after measuring real traffic.
6. **Disk**: `/mnt/ssd1/swayrider/dev-mini/tiles/data` today; a planet is ~140 GB per release and a rollback needs two. The release directory must be on a disk with room for both.
7. **Protomaps terms** for repeated automated downloads and serving derived tiles publicly (still unchecked; also listed in SERVICES.md).
8. **Over-zoom**: confirm the apps' MapLibre version honours `source.maxzoom: 15` (standard behaviour) and show z16+ by over-zooming.

## 8. `swayrider-api` (the gateway in front of `tilesservice`)

Inspected: `cmd/swayrider-api/main.go`, `internal/handlers/{tiles,proxy}.go`, `internal/server/routes.go`, `internal/middleware/{auth,ratelimit}.go`, `README.md`, `API.md`, `api/openapi.yaml`, `review/CODE_REVIEW_2026-08.md`.

### 8.1 How tiles are proxied
- `mux.Handle("/v1/tiles/", middleware.RequireVerifiedUser(s.tiles))`: **any** method and path below the prefix, one handler (`handlers.NewTilesProxy`): `httputil.NewSingleHostReverseProxy` to
  `http://$TILESSERVICE_HOST:$TILESSERVICE_PORT` (default `localhost:8080`), cookies stripped, `Authorization: Bearer <gateway service token>` injected (scopes `region:query routing:execute search:execute tiles:serve`).
- Consequences: new `tilesservice` endpoints (`{tileset}/tiles.json`, `styles/{id}/{variant}`, `fonts/…`, `sprites/…`) are reachable without any gateway change; the reverse proxy forwards request headers
  (`If-None-Match`, `Accept-Encoding`) and passes response headers (`ETag`, `Cache-Control`, `Content-Encoding`, `Vary`) and `304`s unchanged; Range requests are not needed (the service reads the file itself).
- The proxy transport (`newProxyTransport`): dial 5 s, response header timeout 15 s, `MaxIdleConnsPerHost` 10. A map view fires dozens of parallel tile requests, more than 10 idle connections per host, so the gateway
  opens and closes connections in bursts; raise `MaxIdleConnsPerHost` (e.g. 50–100) when tiles are the main traffic. The gateway's own `http.Server` has no read/write timeouts (the 1 s values in `main.go` belong to Redis).

### 8.2 Authentication reaches glyphs and sprites too (decided: all behind auth)
`RequireVerifiedUser` needs a JWT with a verified e-mail, from `Authorization: Bearer` **or the `access_token` cookie** (`API.md`, `openapi.yaml`). Decision 2026-10-05: tiles, styles, fonts and sprites all stay behind it;
no public route, nothing to change in the gateway's routing or in `tilesservice`'s `tiles:serve` check.
- **Web clients** send the cookie to the same origin (or with `credentials: 'include'` cross-origin with `CORS_ALLOWED_ORIGINS` set accordingly) and need nothing extra.
- **Native clients** must add `Authorization: Bearer <token>` to **every** map request, not only tiles: tile URL, style URL, glyph URL (`/fonts/…`) and sprite URL (`/sprites/…`), with MapLibre's `transformRequest` or the platform equivalent. All of them are
  below the same `TilesBaseURL`, so one rule ("requests whose URL starts with the tiles base URL get the header") covers them, and it must also cover the style fetch itself.
- **Token expiry:** the access token lives 15 minutes (`openapi.yaml`). A long map session must refresh the token and use the new one for later requests; a 401 on a glyph or sprite shows up as missing labels or icons, which is easy to mistake for a style bug.
- **Cost of this choice:** a CDN or any shared cache in front of the gateway cannot serve these responses without re-checking auth, so map traffic always reaches the gateway; the long `Cache-Control` of glyphs, sprites and versioned styles still lets the
  *client* cache them. `Cache-Control: public` on authenticated responses is acceptable for the tile data but make sure no intermediary cache is configured in front of the gateway.
- The documentation is inconsistent: `README.md:315` says "No authentication required", the code, `README.md:239` and `API.md:981` say verified user. Fix the README line.

### 8.3 Rate limiting (decided: separate class, per user, generous)
**Today.** `endpointClass` puts `/v1/tiles/*` in class `public`: **per IP, 600 requests/minute**, shared with `/health` and `/api/v1/auth/public-keys` (`RATE_LIMIT_IP_PUBLIC`). One map view is already dozens of requests (tiles at several
zooms; on first load also the style, glyph ranges per font stack and the sprite sheet), so fast panning, several users behind one NAT or several map views on a page reach it; the 429 then also blocks `/health` and key fetching for that IP.

**Middleware order** (`internal/server/server.go:82-85`): `Auth` (parses the token, no rejection) -> `Logging` -> `RateLimit` -> `BodyLimit` -> mux. The limiter therefore already sees the claims, so a per-user limit works without moving anything.

**Change in `swayrider-api`:**
| File | Change |
|---|---|
| `internal/middleware/ratelimit.go` | `endpointClass`: remove `/v1/tiles/` from the `public` case, add `case strings.HasPrefix(path, "/v1/tiles/"): return "tiles", true`. `RateLimitConfig`: new field `UserTiles int`. In the `perUser && authed` branch pick `cfg.UserTiles` for class `tiles` (like `UserExpensive` for `expensive`). Key becomes `rl:tiles:user:<sub>`. |
| `internal/config/config.go`, `internal/server/server.go` | `RateLimitUserTiles: env.GetAsInt("RATE_LIMIT_USER_TILES", 3000)`, passed into `rateCfg` |
| `ratelimit_test.go` | class mapping for `/v1/tiles/…` (styles, fonts, sprites, tiles), limit applies per user and not per IP, unauthenticated requests fall into the per-IP `RATE_LIMIT_IP_API` branch, the `public` class no longer counts tiles |
| `README.md`, `API.md` (table at `API.md:40`, `API.md:981`), `.env`/compose of `infra` | document `RATE_LIMIT_USER_TILES`; tiles are no longer in the `public` class |

**The number.** Starting point **3,000 requests per minute per user** (50/s on average over the sliding window): a cold start is roughly 20–40 tile requests plus the style, 3–10 glyph ranges and the sprite sheet; sustained fast panning is a few
tens of tiles per second for short stretches, well under 3,000 in a minute; a user with phone and web open at the same time shares the budget. That is 5× the whole `public` class today and ~10× a normal busy minute. These are estimates, not
measurements: log the per-user tiles count for a week (the limiter already logs only on exceed; add a debug/metric counter) and set the limit to a comfortable multiple (3–5×) of the observed 99th percentile; the variable exists so it can change
without a release.
- **Unauthenticated** requests to `/v1/tiles/…` fall into the existing per-IP branch (`RATE_LIMIT_IP_API`, 60/min) and are rejected with 401 by `RequireVerifiedUser` anyway: a flood of anonymous tile requests is throttled hard, which is wanted.
- **A client with an expired token** is also "unauthenticated" for the limiter and can hit 60/min before it refreshes: refresh proactively, and treat 401 as "refresh and retry" rather than retrying the burst.
- **429 is costly for a map**: MapLibre does not queue and retry rate-limited tiles, it shows holes until the next move. Keep the limit well above realistic use, and have clients honour `Retry-After` (the gateway sends 60) for glyph/sprite requests.
- **Redis cost:** every tile request adds one limiter call to Redis, and tiles are by far the largest request volume of the gateway. Measure the latency it adds (`REDIS_*` timeouts are 1 s; a slow Redis would slow tiles). If it matters, give the `tiles` class an
  in-process limiter (per instance) or a fixed-window counter; the limiter implementation lives in `swlib` and was not inspected. `RATE_LIMIT_DEGRADE_MODE=memory` (default) keeps tiles working when Redis is down; do not set `deny` while tiles share it.
- **`tilesservice`** itself needs no rate limit (internal network, service token only); it does need the HTTP server timeouts of review finding 1 (5.2) because the gateway is its only client but a stuck connection still holds a goroutine.

### 8.4 Other findings relevant for production
- `API.md`/`openapi.yaml` list only `ping`, `styles`, `styles/{name}` and `{tileset}/{z}/{x}/{y}` as sub-paths; update them with the new endpoints. `ping` through the gateway also needs a verified user (the docs say "public on upstream"), so
  an external uptime check of the tile path needs a token; use the gateway's `/health` for liveness and a separate authenticated synthetic check for `/v1/tiles/<tileset>/0/0/0`.
- `review/CODE_REVIEW_2026-08.md` documents that a single failed service-token refresh once left the gateway on an expired token for weeks and was **the root cause of tile loading failures on the mobile client**.
  Check that the fix is in the deployed version before relying on the tile path; a tile outage then shows up as 401/403 from `tilesservice` while `ping` is fine.
- Tile traffic is authenticated and passes the gateway, so shared caches (CDN) in front of the gateway cannot serve tiles unless the response is marked cacheable for authenticated requests (`Cache-Control: public`
  is already set on tiles; verify the CDN honours it despite the `Authorization`/cookie), which is another reason to keep a stable tile URL and an `ETag`.

## 9. What data-manager has to adjust (done here, on a branch with a pull request)
- `SERVICES.md` and `DESIGN.md` now carry the contract and the decisions of this analysis (Go-template variables, `/v1/tiles` prefix, auth model, gateway rate limit, relative symlink, option (b) and Garage rejected).
- **`styles` stage** (`stages/styles.py`, `services/map_styles.py`, `datamanager/styles/`): today one configuration produces `style-light.json` and `style-dark.json` from the vendored `@protomaps/basemaps` flavors (`datamanager/styles/protomaps-{light,dark,white,black,grayscale,classic,vivid-dark}.json`,
  regenerate with `styles/generate.mjs`); their source is `{"type": "vector", "url": "pmtiles://__TILES__"}`, glyphs `https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf`, sprite `…/sprites/v4/<flavor>`, fonts **Noto Sans Regular / Italic / Medium**.
  Needed: several named styles (id, label, light flavor, dark flavor, label zooms) with a version each; the Go-template source/glyphs/sprite of 4.3 instead of `pmtiles://__TILES__` and the public hosts; `maxzoom: 15`; a `manifest.json`. A new table plus migration for the style definitions, Style tab changes, and the fingerprint/"outdated" logic stay.
- **Glyphs and sprites** become an asset type of the release: source `datamanager/blueprints/configure/static/map-assets/` (14 MB: `fonts/<stack>/<range>.pbf` for the three Noto Sans stacks, `sprites/<flavor>[@2x].{json,png}`), refreshed with `styles/fetch-assets.sh`.
- **Release/deploy step** (Phase 3, not built): assemble `releases/<id>/tiles/` per the contract, flip `current`, keep the previous release; `download.tiles_keep` = 2 so the previous planet build survives for rollback.
- **Public URLs:** the `TilesBaseURL` of the styles is the **gateway's** URL (`https://<api host>/v1/tiles`) filled in by `tilesservice`; Settings → Public URLs only matters for the Style-tab preview and downloads.

## 10. Handover: continuing on a developer machine

### 10.1 What was examined
| Repo | Commit | Notes |
|---|---|---|
| `tilesservice` | `642cb8a` | Go 1.26.2, `github.com/swayrider/tilesservice`, deps `swlib v0.1.10`, `grpcclients v0.1.8`; module sources of `swlib` were not available here (so `compression.SupportsGzip`/`IsGzipped`/`CompressGzip`, `app`, `jwtkeys` were not read) |
| `swayrider-api` | `b1f9ad3` | rate limiter in `swlib` (`ratelimit.Limiter`) not read |
| `infra` | working tree of 2026-10-05 | `dev/layer-20/compose.yml`, `dev-mini/layer-20/compose.yml`, `dev/scripts/deploy-tiles.sh` |
| `data-manager` | this repo | `services/pmtiles.py` (header/metadata reader, a working Python reference for the PMTiles layout), `stages/download_tiles.py`, `stages/styles.py` |
Nothing was built or run in the sibling repos; no Go toolchain was used. Re-check `git log` of both repos before starting, they may have moved.

### 10.2 Pull requests, in order (one repo each)
**tilesservice**
| PR | Content | Done when |
|---|---|---|
| 0 | gzip pass-through fix (5.5) | the 4 new tests and the changed `ServesPreCompressed` test pass; `curl -H 'Accept-Encoding: identity'` returns plain MVT |
| 1 | `internal/pmtiles` reader + tile endpoint for tileset `planet` next to `base`; header-driven zoom limits; `Accept-Encoding` handling reused; ETag | tiles of a fixture file (10.3) are returned byte-identical to the stored tile, 204 for missing/out-of-range tiles, 400 for bad coordinates, `base` unchanged |
| 2 | tileset/release holder, manifest loading, atomic reload (SIGHUP + `current` polling), readiness, `http.Server` timeouts | reload under concurrent requests passes `go test -race`; a broken new release keeps the old one and `ready` reports it |
| 3 | styles: manifest-driven list/variants/versions, template variable `Tileset`, ETag/Cache-Control, routing test of 4.5 | list stays backwards compatible (`name` field), `light`/`dark` aliases work, versioned URL immutable |
| 4 | glyph and sprite handlers | path traversal tests pass; 404 for unknown stacks/ranges; long cache headers |
| 5 | remove tile caches, SQLite/cgo, legacy MBTiles/grid code (after clients moved), Dockerfile static build, docs (`README.md`, `env.example`, delete `CACHE_IMPLEMENTATION.md`, update `tileviewer`) | image builds without gcc; `go mod tidy` shows no sqlite/orb |

**swayrider-api**: one PR with the rate-limit class `tiles` (+ tests + `README.md`/`API.md` + `RATE_LIMIT_USER_TILES`), the `MaxIdleConnsPerHost` bump and the openapi/README corrections (§8). It can ship before the tilesservice PRs.
**infra**: compose changes with PR 5 of tilesservice: mount `TILES_ROOT` (the directory holding `current`) read-only at `/data/tiles` (or a new path), set `TILES_ROOT`, drop `TILESSERVICE_COMPRESSION_*`, `TILESSERVICE_DISK_CACHE_*` and the cache volume, add `RATE_LIMIT_USER_TILES` to the gateway; replace `dev/scripts/deploy-tiles.sh` by the data-manager release step.
**Apps/clients** (not in this workspace): style list + variant by system theme, credentials on every map request (8.2), `maxzoom: 15` over-zoom, token refresh before expiry.

### 10.3 Test fixtures without the full planet
- **A tiny `.pmtiles`:** the approved planet is the `tiles:planet` download: on this machine `/mnt/ssd2/swayrider/data/downloads/tiles/planet/<UTC version>/20261001.pmtiles` (138.5 GB, build of 2026-10-01; `DATA_ROOT/downloads/tiles/planet/…`, stage `download-tiles`). On a developer machine without that file use the remote build instead. With the `pmtiles` CLI (go-pmtiles) cut a few square km: `pmtiles extract <planet.pmtiles> fixture.pmtiles --bbox=4.3,50.8,4.5,50.9 --maxzoom=15` (Brussels centre, a few MB);
  the CLI can also read the remote build (`https://build.protomaps.com/YYYYMMDD.pmtiles`). Commit only if small (a few hundred KB: use a tighter bbox and `--maxzoom=12`), otherwise generate it in the test setup or download it in CI.
- **Pure unit fixtures:** write a PMTiles v3 file in the test with a small writer (header + one root directory + a few gzip-compressed fake tiles); this also covers leaf directories and run-lengths, which a real extract may not exercise. The Python reader `datamanager/services/pmtiles.py` in data-manager shows the header and directory layout in working code.
- **A sample release directory** for the style/glyph/sprite tests: manifest per SERVICES.md, two tiny style templates (`light.json`/`dark.json` with the 4.3 source/glyphs/sprite), two glyph PBFs and one sprite sheet copied from `datamanager/blueprints/configure/static/map-assets/` (e.g. `fonts/Noto Sans Regular/0-255.pbf`, `sprites/light.json`/`light.png`).

### 10.4 PMTiles v3 essentials (for an own reader; verify against the spec `protomaps/PMTiles` → `docs/v3/spec.md`)
- **Header, 127 bytes, little endian:** `"PMTiles"` (7 bytes) + version `3`; then uint64 each: root directory offset, root directory length, metadata offset, metadata length, leaf directories offset, leaf directories length, tile data offset, tile data length, number of addressed tiles, number of tile entries,
  number of tile contents; then uint8: clustered, internal compression, tile compression, tile type, min zoom, max zoom; then int32 (×10⁷ degrees): min lon, min lat, max lon, max lat; uint8 center zoom; int32 center lon, center lat.
  Compression enum: 0 unknown, 1 none, 2 gzip, 3 brotli, 4 zstd. Tile type enum: 1 = MVT. Require `tile type` 1 and `tile compression` 2 (gzip) at open.
- **Directory** (compressed with the *internal* compression, normally gzip): varint entry count, then column-wise varint arrays: tile-id deltas, run lengths, lengths, offsets (an offset of 0 means "right after the previous entry", otherwise value−1). A run length of 0 marks a **leaf directory** pointer (offset/length into the leaf directories section) instead of a tile.
- **Tile id:** a Hilbert-curve index over all zoom levels: `id = Σ_{i<z} 4^i + hilbert(z, x, y)`; look the id up in the directory by binary search, following leaf directories, and read `length` bytes at `tile data offset + offset`.
- **Metadata** (JSON, internal compression): `vector_layers`, `attribution`, build date/schema info (`planetiler:*`/`version` keys: *verify the exact keys on the delivered file*).

### 10.5 Environment variables
| Variable | Status | Meaning |
|---|---|---|
| `TILES_ROOT` | new | directory holding the `current` symlink and `releases/` (read-only mount) |
| `TILES_PATH` | kept during the migration | legacy MBTiles directory for tileset `base` |
| `STYLES_PATH` | kept as fallback, then removed | legacy style files baked into the image |
| `SERVICE_HOST`, `SERVICE_PORT`, `SERVICE_PREFIX` | unchanged | the **gateway's** public URL and `/v1/tiles` → `TilesBaseURL` |
| `COMPRESSION_*`, `DISK_CACHE_*` | deprecated | accepted and ignored for one release, with a deprecation warning, then removed |
| `RELOAD_POLL_SECONDS` | new (name free) | how often the `current` symlink target is polled (0 = only SIGHUP) |
| gateway: `RATE_LIMIT_USER_TILES` | new | per-user requests/minute for `/v1/tiles/*` (default 3000) |

### 10.6 Verification checklist for the whole change
`go test -race ./...` in tilesservice; routing test of 4.5; through the gateway with a real token: `GET /v1/tiles/styles`, a style variant, `/v1/tiles/planet/tiles.json`, a tile (`--compressed` and `Accept-Encoding: identity`), a glyph, a sprite; 401 without a token for each of them;
a release switch while a request loop runs (no failed requests, new `ETag` afterwards); rollback by flipping `current` back; 429 behaviour of the `tiles` class at a low test limit; the apps show labels and icons (glyph/sprite auth) and switch light/dark with the system theme.
