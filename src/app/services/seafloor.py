"""The seafloor explorer's depth grid around a point, from whichever public bathymetry service
knows the point best.

Five sources (`SOURCES`). Each is first asked how fine its data is at the point (`probe`),
which for most is a coverage table and for NOAA a request; they are then tried finest first
(`order_by_cell_size`), and the first grid that is mostly readings and at least partly water is
the answer. Every source considered is recorded as an attempt, so the client can say which one
answered and why the others did not. The rule and its thresholds came from the web app, which
fetched all of this itself until the API took it over; they are kept as they were.

Every source is keyless but KHOA's (`KHOA_API_KEY`). Every request goes through
`fetch_capped`, so each has a deadline and a byte cap, and nothing here logs a URL: KHOA's
carries the instance's service key.

An answer is a fact about the world rather than about the caller, so it is cached under a
global key, as the geocoder's are: for a week when it is `ok` or `dry`, and not at all when it
`failed`, which is worth asking again at once.
"""

import asyncio
import io
import json
import logging
import math
import re
import zlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

import anyio
from PIL import Image

from ..core.config import settings
from ..core.utils.outbound import Failed, FetchFailure, fetch_capped, read_cached, write_cached
from ..schemas.seafloor import (
    SeafloorAttempt,
    SeafloorGrid,
    SeafloorLandHeightFrom,
    SeafloorOutcome,
    SeafloorRead,
    SeafloorSourceId,
)
from . import vector_tiles
from .seafloor_grid import (
    Bounds,
    DepthPoint,
    Grid,
    LandRule,
    NoCoverageError,
    TerrainTile,
    TilePolygon,
    WaterTest,
    WaterTile,
    bounds_around,
    cell_size_meters,
    crop_to_bounds,
    depth_at,
    downsample,
    grid_points,
    known_fraction,
    order_by_cell_size,
    parse_erddap_grid,
    parse_esri_ascii,
    parse_float32_tiff,
    parse_khoa_page,
    terrain_sampler,
    terrarium_elevation,
    tiles_covering,
    to_number,
    water_test,
    wet_fraction,
)

logger = logging.getLogger(__name__)

# The widest view asked for, in metres a side. A dive site needs no more, and it keeps every
# source's request small: one page of KHOA's API, for one.
MAX_SPAN_METERS = 2000

# A grid is used only when at least this share of its cells are readings, and at least this
# share of those are water. Below the first a source has patchy coverage there; below the
# second the point is inland, and a source that knows the land is worth nothing to a seascape.
MIN_KNOWN_FRACTION = 0.6
MIN_WET_FRACTION = 0.1
# Fewer cells than this on a side is no surface to draw: ETOPO at a 1 km span.
MIN_CELLS = 6
# The most cells a side the answer has. Finer data is averaged down to it.
MAX_GRID_DIM = 160
# Pixels a side asked of NOAA's `exportImage`, which resamples to whatever it is asked: exactly
# what the answer holds, so nothing is averaged away twice.
NOAA_REQUEST_DIM = MAX_GRID_DIM

# Far above any honest answer: the largest is a zoom-14 vector tile over a city, about a
# megabyte, and a 160-pixel float TIFF is 100 KB.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# ~10 m. The point is rounded to this before both the cache key and every request, so a second
# look at the same spot is a cache hit.
_COORDINATE_DECIMALS = 4

# Bumped whenever the answer's shape or the way it is reached changes, which a cached answer
# would otherwise outlive.
_CACHE_VERSION = "v1"
# A survey does not change in a week, and a cache miss can spend a dozen third-party requests.
_SETTLED_TTL_SECONDS = 7 * 24 * 60 * 60

# Bound on an upstream message carried into an attempt or a log line.
_MESSAGE_MAX_LENGTH = 200


class NotConfiguredError(Exception):
    """A source that needs configuration it has not been given."""


class SourceFailure(Exception):
    """A request that produced no usable answer; the message is what the attempt reports."""


@dataclass(frozen=True, slots=True)
class Source:
    id: SeafloorSourceId
    # Whether a dry grid from it proves the point is on land.
    is_global: bool
    # How fine its data is at the point, in metres, or None when it has none there. Raises when
    # it could not tell, which is a failure rather than a decline.
    probe: Callable[[float, float], Awaitable[float | None]]
    fetch: Callable[[float, float, Bounds], Awaitable[Grid]]
    # The share of cells that must have a reading. A source whose gaps are simply land (it only
    # lists soundings) sets 0, so a coastal view is not refused for its coast.
    min_known_fraction: float = MIN_KNOWN_FRACTION
    # Never passed by a finer-claiming source: see `order_by_cell_size`.
    holds_place: bool = False


def _short(text: str) -> str:
    """An upstream message as an attempt or a log line may carry it: one line, and bounded."""
    return " ".join(text.split())[:_MESSAGE_MAX_LENGTH]


def _failure_text(failed: Failed) -> str:
    match failed.reason:
        case FetchFailure.STATUS:
            return f"HTTP {failed.status}"
        case FetchFailure.DEADLINE:
            return "timed out"
        case FetchFailure.TOO_LARGE:
            return "answer too large"
        case FetchFailure.TRANSPORT:
            error = failed.error or "error"
            return "timed out" if error.endswith("Timeout") else f"connection failed ({error})"


async def _get(url: str, timeout: float, params: dict[str, str] | None = None) -> bytes:
    """The body of a 2xx answer, or a `SourceFailure` saying why there is none.

    `timeout` is the whole request's, as the web app's `AbortSignal.timeout` was. Redirects are
    followed, as a browser's `fetch` follows them.
    """
    fetched = await fetch_capped(
        url,
        params=params,
        timeout=timeout,
        deadline=timeout,
        max_bytes=_MAX_RESPONSE_BYTES,
        accept=range(200, 300),
        follow_redirects=True,
    )
    if isinstance(fetched, Failed):
        raise SourceFailure(_failure_text(fetched))
    return fetched.body


def _json(body: bytes | str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise SourceFailure("answer is not JSON") from None


async def _all[T](awaitables: Sequence[Awaitable[T]]) -> list[T]:
    """Every result in order, or the first exception raised - with the rest cancelled rather
    than left running, which is the difference from a bare `gather`."""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# --- NOAA NCEI coastal DEMs -----------------------------------------------------------------
# Coastal elevation models down to a few metres, merged into one ArcGIS image service. Only
# around the US and a handful of islands; elsewhere the service holds nothing but the ETOPO
# background, which the ETOPO source serves directly.

NOAA_DEM = "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer"
# Coarser than this, the mosaic is only offering its ETOPO background.
NOAA_USEFUL_CELL_METERS = 50
# Where the mosaic holds a DEM finer than 50 m, padded about a degree, as (min lat, max lat,
# min lon, max lon). Outside these it is not worth asking.
NOAA_COVERAGE = (
    (23, 50, -128, -63),  # US mainland, Bermuda, Bahamas
    (50, 72, -180, -129),  # Alaska, through the central Aleutians
    (50, 56, 171, 180),  # western Aleutians
    (17, 30, -180, -153),  # Hawaii
    (16.5, 19.5, -68.5, -63.5),  # Puerto Rico, Virgin Islands
    (11, 14, -63, -60.5),  # Grenada
    (12, 21, 143.5, 147),  # Guam, Northern Mariana Islands
    (18.5, 20, 166, 167.5),  # Wake Island
    (-15.5, -10.5, -172, -168),  # American Samoa
    (-23, -15.5, -161, -148),  # Cook and Society Islands
    (-3, 3, -93, -86.5),  # Galapagos
)


async def _noaa_probe(lat: float, lon: float) -> float | None:
    covered = any(
        min_lat <= lat <= max_lat and min_lon <= lon <= max_lon for min_lat, max_lat, min_lon, max_lon in NOAA_COVERAGE
    )
    if not covered:
        return None

    params = {
        "geometry": json.dumps({"x": lon, "y": lat}, separators=(",", ":")),
        "geometryType": "esriGeometryPoint",
        "returnCatalogItems": "true",
        # The default of three hides a site's DEM behind the ETOPO background layers.
        "maxItemCount": "25",
        "f": "json",
    }
    body = _json(await _get(f"{NOAA_DEM}/identify", 15, params))
    catalog = body.get("catalogItems") if isinstance(body, dict) else None
    features = catalog.get("features") if isinstance(catalog, dict) else None
    # LowPS is an item's finest pixel, in degrees.
    finest = math.inf
    for item in features if isinstance(features, list) else []:
        attributes = item.get("attributes") if isinstance(item, dict) else None
        degrees = to_number(attributes.get("LowPS", math.nan)) if isinstance(attributes, dict) else math.nan
        if degrees > 0:
            finest = min(finest, degrees)
    if not math.isfinite(finest):
        return None
    meters = cell_size_meters(finest, finest, lat)
    return meters if meters <= NOAA_USEFUL_CELL_METERS else None


async def _noaa_fetch(lat: float, lon: float, bounds: Bounds) -> Grid:
    params = {
        "bbox": f"{bounds.west},{bounds.south},{bounds.east},{bounds.north}",
        "bboxSR": "4326",
        "imageSR": "4326",
        "size": f"{NOAA_REQUEST_DIM},{NOAA_REQUEST_DIM}",
        "format": "tiff",
        "pixelType": "F32",
        "noDataInterpretation": "esriNoDataMatchAny",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }
    body = await _get(f"{NOAA_DEM}/exportImage", 15, params)
    return parse_float32_tiff(body, bounds, lat, lon)


# --- EMODnet Bathymetry ---------------------------------------------------------------------
# The European seas' surveyed digital terrain model, about 115 m, plus one Caribbean tile. Its
# vertical datum is lowest astronomical tide rather than mean sea level.

EMODNET_ERDDAP = "https://erddap.emodnet.eu/erddap/griddap"
# (min lat, max lat, min lon, max lon, dataset), the first that holds the point wins.
EMODNET_TILES = (
    (11, 19, -70.5, -59.5, "bathymetry_dtm_carib_2024"),
    (15, 90, -36, 43, "bathymetry_dtm_2024"),
)


def _emodnet_dataset(lat: float, lon: float) -> str | None:
    for min_lat, max_lat, min_lon, max_lon, dataset in EMODNET_TILES:
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return dataset
    return None


def _erddap_url(base: str, dataset: str, variable: str, bounds: Bounds) -> str:
    # ERDDAP's subset syntax goes in the query string as it is, brackets percent-encoded the
    # way `encodeURI` encodes them; it is not a `key=value` query, so it cannot go in `params`.
    subset = f"[({bounds.south}):({bounds.north})][({bounds.west}):({bounds.east})]"
    return f"{base}/{dataset}.json?{variable}{quote(subset, safe=";,/?:@&=+$!*'()#")}"


async def _emodnet_probe(lat: float, lon: float) -> float | None:
    return 115 if _emodnet_dataset(lat, lon) else None


async def _emodnet_fetch(lat: float, lon: float, bounds: Bounds) -> Grid:
    dataset = _emodnet_dataset(lat, lon)
    if dataset is None:
        raise SourceFailure("outside EMODnet coverage")
    body = await _get(_erddap_url(EMODNET_ERDDAP, dataset, "elevation", bounds), 15)
    return parse_erddap_grid(_json(body), lat, lon)


# --- KHOA water depth -----------------------------------------------------------------------
# The Korea Hydrographic and Oceanographic Agency's depth set for natural science and ocean
# models (the same points as its BADA 2024 file), through the Public Data Portal: soundings
# about 144 m apart over Korean waters, depth in metres from mean sea level. Needs a data.go.kr
# service key; the key's quota is 10,000 requests a day.

KHOA_API = "https://apis.data.go.kr/1192136/waterDepth/GetWaterDepthApiService"
KHOA_COVERAGE = (30.3, 38.5, 122.8, 133)
KHOA_SPACING_METERS = 144
KHOA_PAGE_SIZE = 300
# A 2 km view is about 200 soundings, one page. The cap keeps a runaway from spending more than
# this many requests of the daily quota on one view.
KHOA_MAX_PAGES = 10
# Gridded every 16 m, each cell from the soundings within one and a half spacings. Far finer
# than the soundings, for the coastline from map data: a channel as narrow as the 32 m between
# Munseom and its islet still gets a row of water cells, so the two stay apart. A 2 km view is
# then 126 cells a side.
KHOA_GRID_METERS = 16
KHOA_RADIUS_METERS = KHOA_SPACING_METERS * 1.5
# When the map's coastline can't be had: the soundings sit on a lattice 144-155 m apart with
# none on land, so in open water no spot is farther than half a lattice diagonal (about 110 m)
# from one. A spot farther than this is a missing sounding: land.
KHOA_LAND_BEYOND_METERS = 115
# Soundings are asked for this far past the view on every side, so the view's own edges are not
# mistaken for land for want of the soundings just outside them. A 2 km view plus this margin is
# still about 250 soundings: one page.
KHOA_FETCH_MARGIN_METERS = 180
# Where the portal's XML error names what went wrong.
_KHOA_XML_MESSAGE = re.compile(r"<(returnAuthMsg|errMsg|resultMsg)>([^<]*)<")


def _khoa_key() -> str | None:
    """The service key as it goes in the URL: data.go.kr issues it encoded or decoded."""
    secret = settings.KHOA_API_KEY
    key = secret.get_secret_value().strip() if secret else ""
    if not key:
        return None
    # What `encodeURIComponent` leaves alone, so a decoded key is encoded the way it always was.
    return key if "%" in key else quote(key, safe="-_.!~*'()")


async def _khoa_page(key: str, bounds: Bounds, page: int) -> tuple[float, list[DepthPoint]]:
    # More than five decimals is refused with "invalid request parameter". Assembled by hand
    # so the already-encoded key is not encoded a second time.
    url = (
        f"{KHOA_API}?serviceKey={key}&type=json"
        f"&ymin={bounds.south:.5f}&ymax={bounds.north:.5f}"
        f"&xmin={bounds.west:.5f}&xmax={bounds.east:.5f}"
        f"&pageNo={page}&numOfRows={KHOA_PAGE_SIZE}"
    )
    text = (await _get(url, 10)).decode("utf-8", errors="replace")
    # The portal answers a key or quota problem in XML whatever `type` asked for.
    if text.lstrip("﻿ \t\r\n").startswith("<"):
        match = _KHOA_XML_MESSAGE.search(text)
        raise SourceFailure(f"KHOA {match[2] if match else 'answered XML'}")
    total, points = parse_khoa_page(_json(text))
    return total, points


async def _khoa_probe(lat: float, lon: float) -> float | None:
    min_lat, max_lat, min_lon, max_lon = KHOA_COVERAGE
    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
        return None
    if _khoa_key() is None:
        raise NotConfiguredError("KHOA_API_KEY is not set")
    return KHOA_SPACING_METERS


async def _khoa_fetch(lat: float, lon: float, bounds: Bounds) -> Grid:
    key = _khoa_key()
    if key is None:
        raise NotConfiguredError("KHOA_API_KEY is not set")
    margin_lat = KHOA_FETCH_MARGIN_METERS / 110540
    margin_lon = KHOA_FETCH_MARGIN_METERS / (111320 * math.cos(math.radians(lat)))
    wider = Bounds(
        north=bounds.north + margin_lat,
        south=bounds.south - margin_lat,
        east=bounds.east + margin_lon,
        west=bounds.west - margin_lon,
    )
    total, points = await _khoa_page(key, wider, 1)
    pages = min(math.ceil(total / KHOA_PAGE_SIZE), KHOA_MAX_PAGES) if math.isfinite(total) else 1
    for _, more in await _all([_khoa_page(key, wider, page) for page in range(2, pages + 1)]):
        points.extend(more)
    if not points:
        raise NoCoverageError("no KHOA soundings here")

    # KHOA has soundings and nothing on land: the coastline comes from OpenStreetMap and land
    # heights from AWS Terrain Tiles, asked for at once. Neither can fail the fetch.
    is_water, land = await asyncio.gather(_load_water_test(bounds), _load_land_heights(lat, lon, wider))
    deepest = max(point.depth for point in points)
    # With neither terrain source, land is a low, flat rise: a tenth of the deepest sounding,
    # at least 2 m, enough to stand clear of the water surface.
    flat_land = -max(2, deepest * 0.1)
    rule = LandRule(
        depth=land[0] if land else flat_land,
        beyond_meters=KHOA_LAND_BEYOND_METERS,
        is_water=is_water,
    )
    # Tens of thousands of cells, each weighing its neighbours and asking the coastline nine
    # times: a few hundred milliseconds of Python, kept off the event loop.
    grid = await anyio.to_thread.run_sync(
        lambda: grid_points(points, bounds, KHOA_GRID_METERS, KHOA_RADIUS_METERS, lat, lon, rule)
    )
    return replace(grid, land_height_from=land[1] if land else "flat")


# --- Land heights ---------------------------------------------------------------------------
# AWS Terrain Tiles (Tilezen's Terrarium tiles on AWS Open Data): keyless, about 9 m a pixel at
# Korea's latitude on zoom 14, from SRTM and other public elevation models. Their sea is not to
# be trusted (around Jeju it reads about -1,060 m), so they are only ever asked about cells the
# coastline already calls land. GMRT, coarser at about 60 m, stands in when the tiles can't be
# had.

TERRAIN_TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TERRAIN_ZOOM = 14
# Where the coastline calls a cell land but the terrain still reads sea level or below, the
# shore, the cell gets a token rise so it stands clear of the water.
LAND_SHORE_METERS = 2


def _as_land_depth(elevation: float | None) -> float:
    return -elevation if elevation is not None and elevation > LAND_SHORE_METERS else -LAND_SHORE_METERS


async def _load_land_heights(
    lat: float, lon: float, bounds: Bounds
) -> tuple[Callable[[float, float], float], SeafloorLandHeightFrom] | None:
    """A height for each land cell, as a negative depth, and which source gave it."""
    try:
        tiles = await _all([_terrain_tile(x, y) for x, y in tiles_covering(bounds, TERRAIN_ZOOM)])
        sample = terrain_sampler(tiles, TERRAIN_ZOOM)
        return (lambda la, lo: _as_land_depth(sample(la, lo))), "aws_terrain"
    except Exception as exc:
        logger.warning("Seafloor: no land heights from AWS Terrain Tiles (%s).", _short(str(exc) or type(exc).__name__))

    try:
        grid = await _gmrt_fetch(lat, lon, bounds)
    except Exception as exc:
        logger.warning("Seafloor: no land heights from GMRT either (%s).", _short(str(exc) or type(exc).__name__))
        return None

    def from_gmrt(la: float, lo: float) -> float:
        depth = depth_at(grid, la, lo)
        return _as_land_depth(None if depth is None else -depth)

    return from_gmrt, "gmrt"


async def _terrain_tile(x: int, y: int) -> TerrainTile:
    url = TERRAIN_TILES.format(z=TERRAIN_ZOOM, x=x, y=y)
    png = await _get(url, 10)
    return await anyio.to_thread.run_sync(_decode_terrain_tile, png, x, y)


def _decode_terrain_tile(png: bytes, x: int, y: int) -> TerrainTile:
    with Image.open(io.BytesIO(png)) as image:
        rgb = image.convert("RGB")
    raw = rgb.tobytes()
    elevations = [terrarium_elevation(r, g, b) for r, g, b in zip(raw[0::3], raw[1::3], raw[2::3], strict=True)]
    return TerrainTile(x=x, y=y, size=rgb.width, elevations=elevations)


# --- Coastline from OpenStreetMap -----------------------------------------------------------
# KHOA lists soundings and nothing on land, so where land is has to come from somewhere else.
# OpenFreeMap's vector tiles (OpenStreetMap data in the OpenMapTiles schema) carry the sea as
# polygons, islands as their holes: the coastline the web app's own 2D map draws.

OPENFREEMAP_TILEJSON = "https://tiles.openfreemap.org/planet"
# The tiles' finest zoom: about 2 km a side at Korea's latitude, so a view touches at most four
# of them.
COAST_ZOOM = 14
# Sea, and harbour basins open to it. Lakes, ponds and rivers are land to a seascape.
SEA_CLASSES = frozenset({"ocean", "dock"})


async def _load_water_test(bounds: Bounds) -> WaterTest | None:
    """Water polygons for the box, or None if any tile couldn't be had."""
    try:
        tilejson = _json(await _get(OPENFREEMAP_TILEJSON, 10))
        templates = tilejson.get("tiles") if isinstance(tilejson, dict) else None
        template = templates[0] if isinstance(templates, list) and templates else None
        if not isinstance(template, str) or not template:
            return None

        async def tile(x: int, y: int) -> WaterTile:
            url = template.replace("{z}", str(COAST_ZOOM)).replace("{x}", str(x)).replace("{y}", str(y))
            body = await _get(url, 10)
            return await anyio.to_thread.run_sync(_water_tile, body, x, y)

        return water_test(await _all([tile(x, y) for x, y in tiles_covering(bounds, COAST_ZOOM)]))
    except Exception as exc:
        logger.warning("Seafloor: no coastline from OpenFreeMap (%s).", _short(str(exc) or type(exc).__name__))
        return None


def _water_tile(body: bytes, x: int, y: int) -> WaterTile:
    # A tile served gzipped without saying so in `Content-Encoding` arrives still compressed.
    if body[:2] == b"\x1f\x8b":
        body = zlib.decompress(body, wbits=31)
    layer = vector_tiles.read_layer(body, "water")
    polygons = []
    # A tile with no water layer is all land.
    for feature in layer.features if layer else []:
        if feature.type != vector_tiles.POLYGON or str(feature.properties.get("class")) not in SEA_CLASSES:
            continue
        rings = [[(float(px), float(py)) for px, py in ring] for ring in feature.rings()]
        polygons.append(TilePolygon(rings=rings))
    return WaterTile(z=COAST_ZOOM, x=x, y=y, extent=layer.extent if layer else 4096, polygons=polygons)


# --- GMRT -----------------------------------------------------------------------------------
# Global Multi-Resolution Topography: a global synthesis at its finest where multibeam surveys
# have been merged in. Asked for its maximum resolution.

GMRT_GRID = "https://www.gmrt.org/services/GridServer"


async def _gmrt_probe(lat: float, lon: float) -> float | None:
    return 60


async def _gmrt_fetch(lat: float, lon: float, bounds: Bounds) -> Grid:
    params = {
        "north": f"{bounds.north:.6f}",
        "south": f"{bounds.south:.6f}",
        "east": f"{bounds.east:.6f}",
        "west": f"{bounds.west:.6f}",
        "format": "esriascii",
        "resolution": "max",
    }
    body = await _get(GMRT_GRID, 15, params)
    return parse_esri_ascii(body.decode("utf-8", errors="replace"), lat, lon)


# --- ETOPO 2022 -----------------------------------------------------------------------------
# NOAA's global relief model at 15 arc-seconds, about 450 m: the last resort, and the one that
# answers everywhere. Two ERDDAP servers carry it.

ETOPO_HOSTS = (
    "https://coastwatch.pfeg.noaa.gov/erddap/griddap",
    "https://oceanwatch.pifsc.noaa.gov/erddap/griddap",
)
# Asked for at least this span, so a small box still gets a few of its coarse cells.
ETOPO_MIN_SPAN_METERS = 10_000


async def _etopo_probe(lat: float, lon: float) -> float | None:
    return 450


async def _etopo_fetch(lat: float, lon: float, bounds: Bounds) -> Grid:
    span = max((bounds.north - bounds.south) * 110540, ETOPO_MIN_SPAN_METERS)
    wide = bounds_around(lat, lon, span)
    last: Exception = SourceFailure("no ETOPO host")
    for host in ETOPO_HOSTS:
        try:
            body = await _get(_erddap_url(host, "ETOPO_2022_v1_15s", "z", wide), 15)
            return parse_erddap_grid(_json(body), lat, lon)
        except Exception as exc:
            last = exc
    raise last


# The order sources are preferred in, before cell size reorders them.
SOURCES = (
    Source("noaa_dem", is_global=False, probe=_noaa_probe, fetch=_noaa_fetch),
    Source("emodnet", is_global=False, probe=_emodnet_probe, fetch=_emodnet_fetch),
    Source("khoa", is_global=False, probe=_khoa_probe, fetch=_khoa_fetch, min_known_fraction=0, holds_place=True),
    Source("gmrt", is_global=True, probe=_gmrt_probe, fetch=_gmrt_fetch),
    Source("etopo2022", is_global=True, probe=_etopo_probe, fetch=_etopo_fetch),
)


# --- Choosing -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidate:
    source: Source
    cell_size_meters: float

    @property
    def holds_place(self) -> bool:
        return self.source.holds_place


def _error_text(exc: Exception) -> str:
    return _short(str(exc) or type(exc).__name__)


def _attempt(
    source: Source,
    outcome: SeafloorOutcome,
    *,
    cell: float | None = None,
    grid: Grid | None = None,
    known: float | None = None,
    wet: float | None = None,
    error: str | None = None,
) -> SeafloorAttempt:
    return SeafloorAttempt(
        source=source.id,
        outcome=outcome,
        cell_size_meters=cell,
        cols=grid.cols if grid else None,
        rows=grid.rows if grid else None,
        resolution_meters=grid.resolution_meters if grid else None,
        known_fraction=known,
        wet_fraction=wet,
        error=error,
    )


def _grid_read(grid: Grid) -> SeafloorGrid:
    return SeafloorGrid(
        origin_lat=grid.origin_lat,
        origin_lon=grid.origin_lon,
        cell_lat=grid.cell_lat,
        cell_lon=grid.cell_lon,
        rows=grid.rows,
        cols=grid.cols,
        # To the centimetre, which no source is finer than, and which halves the answer. The
        # `+ 0.0` turns a negated zero back into a plain one.
        depths=[None if depth is None else round(depth, 2) + 0.0 for depth in grid.depths],
        resolution_meters=grid.resolution_meters,
        center_lat=grid.center_lat,
        center_lon=grid.center_lon,
        land_from=grid.land_from,
        land_height_from=grid.land_height_from,
    )


async def _probe(source: Source, lat: float, lon: float) -> float | None | Exception:
    try:
        return await source.probe(lat, lon)
    except Exception as exc:
        return exc


async def _resolve(lat: float, lon: float, span_meters: int) -> SeafloorRead:
    bounds = bounds_around(lat, lon, min(span_meters, MAX_SPAN_METERS))
    attempts: list[SeafloorAttempt] = []

    failures = 0
    candidates: list[_Candidate] = []
    probed = await asyncio.gather(*(_probe(source, lat, lon) for source in SOURCES))
    for source, cell in zip(SOURCES, probed, strict=True):
        if isinstance(cell, NotConfiguredError):
            attempts.append(_attempt(source, "not_configured"))
        elif isinstance(cell, Exception):
            failures += 1
            logger.warning("Seafloor source %s could not be probed: %s", source.id, _error_text(cell))
            attempts.append(_attempt(source, "failed", error=_error_text(cell)))
        elif cell is None:
            attempts.append(_attempt(source, "no_coverage"))
        else:
            candidates.append(_Candidate(source, cell))

    global_says_dry = False
    ordered = order_by_cell_size(candidates)
    for i, candidate in enumerate(ordered):
        source, cell = candidate.source, candidate.cell_size_meters
        try:
            grid = crop_to_bounds(await source.fetch(lat, lon, bounds), bounds)
        except NoCoverageError:
            attempts.append(_attempt(source, "no_coverage", cell=cell))
            continue
        except NotConfiguredError:
            attempts.append(_attempt(source, "not_configured", cell=cell))
            continue
        except Exception as exc:
            failures += 1
            logger.warning("Seafloor source %s failed: %s", source.id, _error_text(exc))
            attempts.append(_attempt(source, "failed", cell=cell, error=_error_text(exc)))
            continue

        if grid.rows < MIN_CELLS or grid.cols < MIN_CELLS:
            attempts.append(_attempt(source, "too_coarse", cell=cell, grid=grid))
            continue
        known = known_fraction(grid)
        if known < source.min_known_fraction:
            attempts.append(_attempt(source, "too_little_data", cell=cell, grid=grid, known=known))
            continue
        wet = wet_fraction(grid)
        if wet < MIN_WET_FRACTION:
            global_says_dry = global_says_dry or source.is_global
            attempts.append(_attempt(source, "mostly_land", cell=cell, grid=grid, known=known, wet=wet))
            continue

        attempts.append(_attempt(source, "used", cell=cell, grid=grid, known=known, wet=wet))
        attempts.extend(_attempt(rest.source, "not_needed", cell=rest.cell_size_meters) for rest in ordered[i + 1 :])
        return SeafloorRead(
            status="ok", source=source.id, grid=_grid_read(downsample(grid, MAX_GRID_DIM)), attempts=attempts
        )

    if global_says_dry and failures == 0:
        return SeafloorRead(status="dry", source=None, grid=None, attempts=attempts)
    return SeafloorRead(status="failed", source=None, grid=None, attempts=attempts)


def _round_coordinate(value: float) -> float:
    # Half up, as the web app rounded it, so a point it sends rounded comes back to itself.
    scale = 10.0**_COORDINATE_DECIMALS
    return math.floor(value * scale + 0.5) / scale


def caller_budget_key(user_id: int) -> str:
    """The rate-limit counter of what one account has asked of the seafloor."""
    return f"seafloor:user:{user_id}"


async def resolve_seafloor(lat: float, lon: float, span_meters: int) -> SeafloorRead:
    """The best depth grid for a square `span_meters` across around a point, with a record of
    every source considered.

    Cached under a global key, deliberately not user-scoped: the answer is about the point. The
    key says whether KHOA is configured, since that changes the answer around Korea, and an
    operator who adds the key should not wait a week to see it used.
    """
    lat = _round_coordinate(lat)
    lon = _round_coordinate(lon)
    khoa = "khoa" if _khoa_key() else "nokhoa"
    key = f"seafloor:{_CACHE_VERSION}:{khoa}:{span_meters}:{lat}:{lon}"
    cached = await read_cached(key, SeafloorRead.model_validate)
    if cached is not None:
        return cached

    result = await _resolve(lat, lon, span_meters)
    if result.status != "failed":
        await write_cached(key, result.model_dump_json(), ttl_seconds=_SETTLED_TTL_SECONDS)
    return result
