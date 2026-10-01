# SERVICES.md — changes required in sibling services

Changes that other SwayRider services (separate repos, not part of this workspace) need
when data-manager's build output changes. Everything about `tilesservice` below is derived
from the legacy pipeline output and `infra` docs, **not from the tilesservice code** — items
marked *(verify)* must be checked against that repo before implementation.

## tilesservice — move from L0/L1/L2 to one Protomaps PMTiles tileset

**Why:** data-manager's tiles stage will no longer build tiles. It downloads a regional extract of the
Protomaps daily planet build (`pmtiles extract`) and delivers a single `tiles.pmtiles` (Protomaps basemap
schema, Z0–15) plus light/dark `style.json` files, instead of three levels of custom-schema MBTiles.
Decision of 2026-10-01 (it replaces the earlier plan of building OpenMapTiles-schema MBTiles with planetiler).
See DESIGN.md → "Tiles stage: Protomaps PMTiles extract".

### Today (legacy output, per `data-pipeline` README / `infra/dev/scripts/deploy-tiles.sh`)
- `TILES_DATA_PATH/L0.mbtiles` (Z0–6 world), `TILES_DATA_PATH/L1/{tile}.mbtiles` (Z7–10) and
  `TILES_DATA_PATH/L2/{tile}.mbtiles` (Z11–16), where `{tile}` is a 10° grid cell such as `N50_E000`.
- The service picks the file from zoom + tile coordinate *(verify)*, serves vector tiles with the custom layers
  `land, water, urban, forest, roads, railways, ferries, waterways, highway_labels, places, boundaries, country_labels`.
- Deployed by extracting `tiles.tar` and restarting `tilesservice` (manual, see `deploy-tiles.sh`).

### What the new file is (checked against the live Protomaps builds, 2026-10-01)
- PMTiles **v3**, clustered, gzip-compressed MVT, **Z0–15**, one file covering the configured extent
  (not a grid of files). Metadata JSON holds `vector_layers`; the header holds bounds and zoom range.
- Protomaps basemap schema, layers `boundaries, buildings, earth, landuse, natural, places, pois, roads, transit, water`
  (verify the exact list and fields against the delivered file's metadata); features carry `kind` / `kind_detail`,
  `min_zoom`, and `name` plus `name:xx` translations.
- Built daily by Protomaps from their own OSM snapshot (build date and schema version end up in the file's metadata
  and in data-manager's asset record), not from our Geofabrik extracts.

### Target
1. **Single tileset.** Open one `tiles.pmtiles` (path from config, see 2). Two ways to serve it *(decide with the service
   owner)*:
   - **(a) Server-side reader (recommended).** Read the file with the go-pmtiles library (header, directory lookup, Range
     reads of the file) and keep one tile endpoint, `/{z}/{x}/{y}` (`.mvt` suffix if the client needs one): the tile bytes are
     already gzip-compressed MVT, pass them through with `Content-Encoding: gzip`, `Content-Type: application/x-protobuf`.
     Return 204/404 for missing tiles and **allow requests above the max zoom** (clients over-zoom; the file is Z0–15).
     Clients keep a plain `tiles` URL template, which is what MapLibre Native expects.
   - **(b) Static file with HTTP Range.** Serve `tiles.pmtiles` as an asset (Range, CORS, `ETag`) and let clients read it
     with the `pmtiles://` protocol. No tile endpoint; works for MapLibre GL JS with the `pmtiles` protocol plugin;
     whether MapLibre Native can read it must be verified before choosing this.
   Remove the zoom/grid-cell file selection and the multi-file handle cache in either case.
2. **Config.** `TILES_DATA_PATH` points at a directory containing `tiles.pmtiles` (or directly at the file). The file is
   replaced on every release, never modified in place. Reload the handle on deploy: either a restart (as today) or watch the
   release pointer symlink `current/` *(verify how it is mounted in `infra/dev/layer-20`)*.
3. **TileJSON.** `GET /tiles.json` (or `/{name}.json`) built from the PMTiles header and metadata (`name, bounds, center,
   minzoom, maxzoom, attribution, vector_layers`), with the public `tiles` URL template, so MapLibre can use `"url"` instead of
   hard-coded layer/zoom info. Attribution must contain "© OpenStreetMap contributors" (no OpenMapTiles credit any more;
   keep the Protomaps notice that ships with the styles).
4. **Style, glyphs, sprites** (new; MapLibre needs them and today the style is not served by data-manager):
   - `GET /styles/light.json`, `/styles/dark.json` — served from the release directory (`style-light.json`,
     `style-dark.json`, produced by data-manager for the Protomaps schema with the source/glyphs/sprite URLs filled in).
   - `GET /fonts/{fontstack}/{range}.pbf` — static glyph PBFs (source: the `protomaps/basemaps-assets` repository, Noto Sans
     stacks); shipped as a data directory next to the tiles or embedded.
   - `GET /sprites/{name}[@2x].{json,png}` — sprite sheets from the same assets repository, matching the styles.
   - CORS for the client origins, `Cache-Control: public, max-age` for glyphs/sprites, short cache + `ETag` for style/tiles.
5. **Health/metrics.** Keep the existing health endpoint; expose the active tileset's build date and schema version (from the
   PMTiles metadata) so deploys can verify which release is live.

Note: the styles exported by data-manager (Configure → Style) are Protomaps-schema styles (`@protomaps/basemaps` flavors, source id
`protomaps`, source URL from Settings → Public URLs `public.tiles_url`, e.g. `pmtiles://https://host/tiles.pmtiles`; glyphs/sprite
default to the public `protomaps/basemaps-assets` pages and can be overridden with `public.glyphs_url`/`public.sprite_url` or
`?tiles_url=&glyphs=&sprite=` on the download links).

### Client (MapLibre GL JS / Native)
- Point the map at the new style URL(s) (`/styles/light.json` / `dark.json`, choose by system theme) instead of the
  hand-written style; remove client-side overrides that referenced the old layers (`places`, `roads`, `highway_labels`…).
- Layer/attribute mapping for any client code that queries features: `places` → `places` (Protomaps: `kind`, `kind_detail`,
  `min_zoom`, `population_rank`-style fields *(verify)*, `name:xx`), `roads` and `highway_labels` → `roads` (`kind`,
  `kind_detail`, `ref`, `network` *(verify)*), `water` → `water`, `forest`/`urban` → `landuse`, `natural`, `earth`;
  `boundaries` and `pois`, `transit`, `buildings` are new and optional.
- No more L0/L1/L2 zoom ranges: one source with `maxzoom: 15`.
- **Under option (b):** MapLibre GL JS needs the `pmtiles` protocol plugin registered (`addProtocol`) and a `pmtiles://` source URL;
  MapLibre Native support must be verified first.
- **Lost compared with the legacy tiles** (Protomaps tiles are not ours to change): the yellow motorway ramp colouring
  (`motorway_link_type`), the A/E/N road shield data beyond `ref`/`network`, the legacy `population` string, the 26-language
  Natural Earth country labels (use `name:xx`), the Polsby-Popper forest/urban filtering. A custom layer would require
  building tiles ourselves again, which is the fallback if the schema ever blocks a requirement.

### Rollout
- Run old and new side by side: new endpoints under a versioned prefix (e.g. `/v2/`) while the legacy L0/L1/L2 handler stays.
- data-manager's deploy step switches the active release pointer; rollback = previous release directory.
- Switch the client style URL once the new tileset is verified; then remove the legacy handler and `L1/`, `L2/` directories.
- data-manager no longer needs planetiler, a JRE, tippecanoe or GDAL for tiles; deploy only has to deliver the PMTiles file
  and the two style files (plus glyph/sprite assets when they change).

### Open questions for the tilesservice owner
- Where is the current style JSON kept, and who serves glyphs/sprites today?
- Can tilesservice read PMTiles (go-pmtiles library) and, if the client reads the file itself, does MapLibre Native support
  the `pmtiles://` protocol? This decides option (a) vs (b) above.
- Does the service cache open file handles or read on every request? Any per-file logic tied to `N50_E000` names?
- Is the tileset public (through Traefik) or internal only — affects CORS and the URL template in TileJSON/styles.
- Size and egress: the Z0–15 extract for the configured extent is expected to be several GB (to be measured); is serving
  Range reads of a file that size through Traefik acceptable under option (b)?

## Other services
- **routerservice / Valhalla, searchservice / Pelias:** unaffected by the tile schema change.
- **Pelias (planned, see DESIGN.md → "Locality boundaries"):** the boundary/placeholder patch is a separate change list, tracked there.
- **regionservice:** consumes region definitions; no tile-schema dependency expected *(verify)*.
