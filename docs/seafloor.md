# Seafloor

How `GET /api/v1/seafloor` finds the depth grid around a point: which sources it asks, how it picks
one, and why the list is what it is. The OpenAPI document (`/api/v1/openapi.json`) is the reference
for the request and every field of the answer; this is the background it doesn't carry.

The code is `src/app/services/seafloor.py` (the sources, the resolver and its cache) and
`src/app/services/seafloor_grid.py` (the pure parts: the readers for each source's format, the
gridding, the coastline test and the ordering rule), with `src/app/services/vector_tiles.py`, a
minimal Mapbox Vector Tile reader, for the coastline. The rules and the source list follow the "Site
Seascape" view in [Submersion](https://github.com/submersion-app/submersion).

## How it works

```
client: GET /api/v1/seafloor?lat=&lon=&span=
  └─ resolve_seafloor()                         ← cached for a week, globally
       ├─ ask each source whether it covers the point, and how finely
       ├─ try them finest-first (see "Choosing a source")
       └─ answer the first good grid, plus what every source answered
```

The client sends only a point and a span; it never names a source. **2 km is the widest view**
(`MAX_SPAN_METERS`), and 1 km the other choice: plenty for a dive site, and it keeps every source's
request small, one page of KHOA's API among them. The point is rounded to 4 decimals (about 10 m)
before the cache key and every request, and the answer holds at most **160 cells a side**
(`MAX_GRID_DIM`); finer data is averaged down to it.

**The sources are only ever asked from the server.** KHOA's key must not reach a browser, and two of
the sources send no CORS headers, so a browser couldn't read them anyway. **On a cache miss the
rounded point is sent to those services**; nothing identifying the diver goes with it.

**Answers.** `status` is `ok` (a `grid`, and the `source` it came from), `dry` (on land, no grid) or
`failed` (no source could answer). `ok` and `dry` are 200; `failed` is a 503 with the same body plus
`detail`, so the attempts still arrive. Depths are metres, **positive below sea level**, negative
for land above it, and null where there is no reading. Every source considered gets an attempt, with
an `outcome` and the numbers behind it (its cell size, the grid's cols and rows, the known and wet
fractions, the upstream error), so a client can say why each was or wasn't used.

**Caching.** `ok` and `dry` are cached for **7 days** under a key that is the same for every
account: the answer is about the point, not the caller. `failed` is never cached, so the next try
asks again. The key records whether KHOA is configured, so adding the key takes effect at once.
Cache hits count toward the per-account rate limit (`SEAFLOOR_RATE_LIMIT_PER_USER`, 120 an hour by
default).

**Timeouts.** Each request to NOAA, EMODnet, GMRT or ETOPO gets **15 s**; each to KHOA, the
coastline tiles or the terrain tiles **10 s**. No answer is read past 8 MiB.

## Data sources

Four are keyless. **KHOA needs a data.go.kr service key**, `KHOA_API_KEY` in `src/.env`.

| Source (`source`)                                    | Covers                                    | Detail                            | How it's fetched                                                                         | Licence                        |
| ---------------------------------------------------- | ----------------------------------------- | --------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------ |
| **NOAA coastal elevation models**, NCEI (`noaa_dem`) | US coasts and some islands (11 boxes)     | down to about 3 m                 | ArcGIS image service: `identify` to check coverage, then `exportImage` as a float32 TIFF | US public domain               |
| **EMODnet Bathymetry**, DTM 2024 (`emodnet`)         | European seas, plus one Caribbean area    | about 115 m                       | ERDDAP, JSON                                                                             | CC BY 4.0, credit required     |
| **KHOA** water depth, BADA 2024 (`khoa`)             | Korean waters, 30.3–38.5° N, 122.8–133° E | soundings about 144 m apart       | data.go.kr REST API, JSON, 300 points per page; gridded on the server                    | data.go.kr terms; credit shown |
| **GMRT** (`gmrt`)                                    | everywhere                                | about 60 m (finer where surveyed) | GridServer, ESRI ASCII grid, `resolution=max`                                            | CC BY 4.0, credit required     |
| **ETOPO 2022** (`etopo2022`)                         | everywhere                                | about 450 m                       | ERDDAP, JSON (two servers, the second a fallback)                                        | US public domain               |

Four of them publish elevation (negative under water), flipped once, when reading, into depth,
positive down; land stays as a negative depth. KHOA already publishes depth, positive down, from
mean sea level.

A few details that matter:

- **NOAA:** its image service also holds a coarse global background layer. A point only counts as
  covered when a model finer than **50 m** covers it (`NOAA_USEFUL_CELL_METERS`), and the service is
  asked for up to 25 catalogue items: its default of 3 hides the detailed model behind the
  background. Outside the 11 boxes in `NOAA_COVERAGE` it isn't asked at all. Its `exportImage` is
  asked for exactly 160 × 160 pixels, so nothing is averaged away twice.
- **EMODnet:** depths are measured from lowest astronomical tide, not mean sea level. That's fine
  when EMODnet is used on its own, as it is here; never mix its cells with another source's.
- **KHOA:** see the next section.
- **ETOPO:** the area asked for is always at least **10 km** wide (`ETOPO_MIN_SPAN_METERS`), then
  cropped back to the view. Its cells are 15 arc-seconds, about 460 m north to south everywhere, so
  a 2 km view keeps only **4 or 5 rows**, under the minimum of 6. Its columns narrow with latitude
  (6 at Paris), but the rows decide: **ETOPO is always `too_coarse`**. It stays in the list as the
  last resort should the limit ever rise.

## KHOA (Korea)

The Korea Hydrographic and Oceanographic Agency's water depth set "for natural science", built for
ocean models, served through the Public Data Portal: `GetWaterDepthApiService` at
`https://apis.data.go.kr/1192136/waterDepth/`. **It is the same data as the BADA 2024 file**
(`BADA2024_korea_depth_v1.1.nc`): in a 4 km test box around Beomseom, all 677 points matched it in
position and depth, to the centimetre.

**The key.** Set `KHOA_API_KEY` in `src/.env` (an installed instance: the `.env` beside its compose
file). data.go.kr issues an encoded and a decoded form; either works, and an encoded key (one with a
`%` in it) is used as it is so it isn't encoded twice. The key stays on the server and is never
logged. Without it, KHOA's attempt is `not_configured` and GMRT takes over in Korean waters.

**How the API behaves:**

|         |                                                                                                                                       |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Box     | `ymin`/`ymax`/`xmin`/`xmax`, at most **1°** a side and at most **5 decimals**: 6 is refused with `10 INVALID_REQUEST_PARAMETER_ERROR` |
| Paging  | `numOfRows` up to **300** per page; `totalCount` says how many points there are                                                       |
| No data | Outside Korean waters or on land: `03 NODATA_ERROR`, read as `no_coverage`                                                            |
| Errors  | Key and quota problems can come back as XML even when JSON was asked for; the message goes into the attempt's `error`                 |
| Spacing | about 144 m (144–155 m measured), the same everywhere                                                                                 |

**The quota is 10,000 requests a day.** A 2 km view is about 200 points, **one request**, and a 1 km
view is one too. A point inside Korea's box but on land also costs one request (the `03` answer). A
second look at the same point is a cache hit and costs nothing. A view is capped at **10 pages**
(`KHOA_MAX_PAGES`) so nothing can drain the quota by mistake.

**From points to a grid.** The API returns loose soundings, not a grid. The server lays them on a
regular grid every **16 m** (`KHOA_GRID_METERS`) by inverse-distance weighting of the soundings
within **216 m**, one and a half spacings. The grid is far finer than the soundings so the coastline
below has cells to draw with: a 2 km view is about 125 cells a side (125 × 126 off Munseom).

**The coastline comes from OpenStreetMap.** KHOA has soundings and nothing on land, so where land is
comes from OpenFreeMap's `planet` vector tiles (OpenStreetMap data, OpenMapTiles schema), the same
coastline the web app's 2D map draws. The server fetches the zoom-14 tiles under the view (one to
four), reads their `water` layer (classes `ocean` and `dock`; lakes, ponds and rivers count as
land), and tests each cell at a 3 × 3 spread of spots across it: **any spot on land makes the cell
land**, so an islet narrower than a cell still shows. Off Munseom the small islet north-east of it,
104 × 75 m and about 32 m from Munseom, shows as its own island with water between. The grid's
`land_from` is then `openstreetmap`.

- A water cell with no sounding within 216 m (a gap that only exists because of an island) takes the
  nearest sounding's depth.
- **Land heights come from AWS Terrain Tiles** (Tilezen's Terrarium tiles on AWS Open Data):
  keyless, about 9 m a pixel at zoom 14 around Korea, from SRTM and other public elevation models,
  decoded with Pillow. Each land cell takes the height there, interpolated between pixels; Munseom's
  peak comes out at about 72.4 m (73 m real). **Their sea values are wrong** (around Jeju about
  -1,060 m), so they are only asked about cells the coastline already calls land. Where the
  coastline says land but the terrain reads 2 m or less (the shoreline), the cell gets a token 2 m.
- **If the terrain tiles can't be had, GMRT stands in** (about 60 m and smoother: Munseom comes out
  at about 47 m), and if that fails too, land is a flat rise of a tenth of the deepest sounding (at
  least 2 m). The grid's `land_height_from` says which: `aws_terrain`, `gmrt` or `flat`. It is null
  for every other source.
- The coastline is always OpenFreeMap's, whatever basemap an instance's web app is configured with.

**If the coastline tiles can't be had**, land falls back to **missing soundings**: the soundings sit
on a lattice 144–155 m apart with none on land, and in open water no spot is farther than half a
lattice diagonal (about 110 m) from one, so a cell more than **115 m** from every sounding is land
(`KHOA_LAND_BEYOND_METERS`). That catches an island that removes even one lattice node, but islets
closer than about 144 m to another island merge with it. `land_from` is then `soundings`.

Soundings are fetched **180 m past the view** on every side (`KHOA_FETCH_MARGIN_METERS`), so the
view's edges are never short of neighbours. A 2 km view plus that margin is still one page.

KHOA also skips the 60% rule below (its `min_known_fraction` is 0): its gaps are coast, not missing
data.

**It outranks GMRT in Korean waters.** By the 2× rule GMRT's nominal 60 m would jump ahead of KHOA's
144 m, but KHOA is national survey data and GMRT around Korea looks interpolated. So KHOA holds its
place (`holds_place`), as surveyed regional data should.

## Choosing a source

The rules follow Submersion's:

1. **Ask every source** whether it covers the point, and how fine its data is there. One that
   doesn't is `no_coverage`; KHOA without a key is `not_configured`.

2. **Order the ones that do.** The default order is NOAA, EMODnet, KHOA, GMRT, ETOPO (`SOURCES`). A
   source moves ahead of one before it only if its data is **more than 2× finer**
   (`PREEMPTION_FACTOR`). That's why EMODnet's surveyed 115 m stays ahead of GMRT's 60 m: the 60 m
   is a global synthesis that is often interpolated. KHOA is never passed at all (see above).

3. **Try them in that order**, and use the first grid that has:

   - at least **6 cells** a side (`MIN_CELLS`), else `too_coarse`,
   - a reading in at least **60%** of its cells (`MIN_KNOWN_FRACTION`; KHOA: no minimum), else
     `too_little_data`, and
   - water in at least **10%** of its cells (`MIN_WET_FRACTION`), else `mostly_land`.

   The one that passes is `used`, and the sources after it are `not_needed`. A source that errors or
   times out is `failed`, with the reason in `error`.

4. If none qualifies, and a worldwide source (GMRT or ETOPO) said `mostly_land` with no source
   `failed`, the answer is **`dry`**. Otherwise it is `failed`.

**Spots it was tested on**, all at 2 km, through the live sources on 2026-11-10:

| Spot                   | Point             | Answer     | Grid                 |
| ---------------------- | ----------------- | ---------- | -------------------- |
| Jeju, off Munseom      | 33.2251, 126.5683 | `khoa`     | 125 × 126, 16 m      |
| Jeju, Beomseom         | 33.2175, 126.5144 | `khoa`     | 125 × 126, 16 m      |
| Busan, off Haeundae    | 35.155, 129.17    | `khoa`     | 126 × 126, 16 m      |
| Ulleungdo, south coast | 37.47, 130.88     | `khoa`     | 126 × 126, 16 m      |
| Off San Diego          | 32.85, -117.27    | `noaa_dem` | 160 × 160, 12.5 m    |
| Florida Keys           | 25.13, -80.40     | `noaa_dem` | 160 × 160, 12.5 m    |
| Off Marseille          | 43.21, 5.33       | `emodnet`  | 25 × 19, about 115 m |
| Open Pacific           | 10.0, -150.0      | `gmrt`     | 34 × 34, about 60 m  |
| Paris (inland)         | 48.85, 2.35       | `dry`      | –                    |
| Seoul (inland)         | 37.5665, 126.978  | `dry`      | –                    |

The Florida Keys model is 3.4 m natively; NOAA's service resamples it to the 160 × 160 asked for.
Seoul's `dry` comes from GMRT's `mostly_land`; KHOA answered `03`, which is `no_coverage`.

## Why 4 of Submersion's 5 sources (plus KHOA)

Submersion also uses **swissBATHY3D** from swisstopo, which is detailed (about 2 m) data for the
beds of Swiss lakes. It's left out for two reasons.

**It only covers about 23 Swiss lakes.** No sea spot, and nowhere outside Switzerland, would ever
use it.

**It isn't a "send a box, get a grid" service like the other four.** Getting a grid out of it takes
all of this:

1. **Search swisstopo's STAC catalogue** (`data.geo.admin.ch/api/stac/v1`) for the file that covers
   a lake.
2. **Download a ZIP for the whole lake.** These are large, and Submersion gives them a long timeout.
3. **Unzip it and read the XYZ text points** inside.
4. **Convert from the Swiss coordinate system (LV95)**, which is in metres, not latitude and
   longitude.
5. **Turn heights into depths.** The values are lake-bed heights above sea level, so each lake needs
   its own mean water level, from a table of the lakes.

Submersion's version is about **2,900 lines across 9 files**, including a tile cache, freshness
checks and pre-loading nearby sites. Rebuilding that for spots no sea diver will pick wasn't worth
it. Adding it later is self-contained: one more entry in `SOURCES`, with a coverage box per lake.

## Other data that would need an account

Not used:

- **Copernicus Marine** has a 100 m coastal depth product derived from satellite images
  (`BATHYMETRY_GLO_PHY_COASTAL_L4_MY_016_001`). It needs a free account (username and password) and
  is read through their toolbox, not a simple web request.

Around Jeju, GMRT's "60 m" may not be survey data. Offshore it can be interpolated from coarser
data, which would explain why the seabed there looked very flat before KHOA was added.

## Testing

```bash
ENVIRONMENT=local SECRET_KEY=testsecret uv run pytest tests/test_seafloor.py
curl -s -H "Authorization: Bearer $TOKEN" "localhost:8000/api/v1/seafloor?lat=25.13&lon=-80.40&span=2000" | head -c 300
```

The tests need no network: every source's reader runs on canned responses. The `curl` line asks the
live sources through the running API, signed in.

## Licensing

Submersion is **GPL-3.0**. Nothing here is copied from it: the sources and the rules were
re-implemented from how it works. OpenDiving is AGPL-3.0, which could take GPL-3.0 code with
attribution anyway.

**GMRT and EMODnet require credit (CC BY 4.0)**, as do OpenStreetMap's coastline and the terrain
tiles' sources when KHOA uses them. The answer names the `source`, `land_from` and
`land_height_from` so a client can show the right credit; keep it wherever the view goes.
