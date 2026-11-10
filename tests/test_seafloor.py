"""Tests for the seafloor depth grid - the route (`api/v1/seafloor.py`), the resolver that picks a
source (`services/seafloor.py`), the pure parsers and grid operations under it
(`services/seafloor_grid.py`) and the vector-tile reader the coastline comes through
(`services/vector_tiles.py`).

No test reaches the network: the resolver's tests answer every source from a fake transport
(`_Network`), told apart by host, built from fixtures these helpers write in each source's own
wire format - an ESRI ASCII grid, an ERDDAP table, a float TIFF, a KHOA page, a Terrarium PNG and
a Mapbox Vector Tile.

The parser and selection tests are ports of the web app's own, from when it fetched the seafloor
itself; two more hold the gridding and the coastline test to the naive algorithms they speed up.
"""

import gzip
import io
import logging
import math
import random
import struct
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.services import seafloor, vector_tiles
from src.app.services.seafloor_grid import (
    Bounds,
    DepthPoint,
    Grid,
    LandRule,
    NoCoverageError,
    TerrainTile,
    TilePolygon,
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
    tile_position,
    tiles_covering,
    water_test,
    wet_fraction,
)
from tests.helpers.fake_redis import FakeRedis

_REAL_ASYNC_CLIENT = httpx.AsyncClient

CURRENT_USER = {"id": 7, "username": "ada", "is_superuser": False}

# Points whose sources the coverage tables settle without a request.
OPEN_PACIFIC = (10.0, -150.0)  # GMRT and ETOPO only
JEJU = (33.2251, 126.5683)  # KHOA, then GMRT and ETOPO


# --- Wire-format fixtures -------------------------------------------------------------------


def _grid(rows: int, cols: int, depths: list[float | None] | None = None) -> Grid:
    return Grid(
        origin_lat=0,
        origin_lon=0,
        cell_lat=1,
        cell_lon=1,
        rows=rows,
        cols=cols,
        depths=depths if depths is not None else [10.0] * (rows * cols),
        resolution_meters=100,
        center_lat=rows / 2,
        center_lon=cols / 2,
    )


def _esri_ascii(lat: float, lon: float, *, cells: int = 10, cell: float = 0.002, elevation: float = -4000) -> str:
    """A square ESRI ASCII grid centred on the point, every cell at `elevation`."""
    corner_lat = lat - cells * cell / 2
    corner_lon = lon - cells * cell / 2
    header = f"ncols {cells}\nnrows {cells}\nxllcorner {corner_lon}\nyllcorner {corner_lat}\ncellsize {cell}\n"
    rows = "\n".join(" ".join([str(elevation)] * cells) for _ in range(cells))
    return header + "nodata_value -99999\n" + rows + "\n"


def _erddap(lat: float, lon: float, *, cells: int, cell: float, elevation: float = -3000) -> dict[str, Any]:
    """An ERDDAP griddap `.json` table centred on the point, every cell at `elevation`."""
    start_lat = lat - (cells - 1) * cell / 2
    start_lon = lon - (cells - 1) * cell / 2
    rows = [
        [start_lat + r * cell, start_lon + c * cell, elevation]
        for r in range(cells)  # ERDDAP lists them south to north, but the order is not relied on.
        for c in range(cells)
    ]
    return {"table": {"columnNames": ["latitude", "longitude", "z"], "rows": rows}}


def _strip_tiff(width: int, height: int, values: list[float]) -> bytes:
    """A little-endian, uncompressed, single-strip float32 TIFF, rows north first."""
    entries = [
        (256, 3, width),
        (257, 3, height),
        (258, 3, 32),
        (259, 3, 1),
        (273, 4, 0),  # strip offset, filled in below
        (277, 3, 1),
        (278, 3, height),
        (279, 4, width * height * 4),
        (339, 3, 3),
    ]
    data_offset = 8 + 2 + len(entries) * 12 + 4
    out = bytearray(b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", len(entries)))
    for tag, kind, value in entries:
        stored = data_offset if tag == 273 else value
        out += struct.pack("<HHI", tag, kind, 1)
        out += struct.pack("<HH", stored, 0) if kind == 3 else struct.pack("<I", stored)
    out += struct.pack("<I", 0)
    out += struct.pack(f"<{len(values)}f", *values)
    return bytes(out)


def _tiled_tiff(width: int, height: int, tile: int, values: list[float]) -> bytes:
    """The same, laid out in `tile`-pixel tiles, edge tiles padded past the image as TIFF pads
    them."""
    across = math.ceil(width / tile)
    down = math.ceil(height / tile)
    blocks = []
    for ty in range(down):
        for tx in range(across):
            block = []
            for y in range(tile):
                for x in range(tile):
                    gx, gy = tx * tile + x, ty * tile + y
                    block.append(values[gy * width + gx] if gx < width and gy < height else -9999.0)
            blocks.append(struct.pack(f"<{len(block)}f", *block))
    entries_count = 10
    offsets_at = 8 + 2 + entries_count * 12 + 4
    data_at = offsets_at + 4 * len(blocks)
    offsets = [data_at + i * len(blocks[0]) for i in range(len(blocks))]
    entries = [
        (256, 3, 1, width),
        (257, 3, 1, height),
        (258, 3, 1, 32),
        (259, 3, 1, 1),
        (277, 3, 1, 1),
        (322, 3, 1, tile),
        (323, 3, 1, tile),
        (324, 4, len(blocks), offsets_at),
        (325, 4, 1, len(blocks[0])),
        (339, 3, 1, 3),
    ]
    out = bytearray(b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", len(entries)))
    for tag, kind, n, value in entries:
        out += struct.pack("<HHI", tag, kind, n)
        out += struct.pack("<HH", value, 0) if kind == 3 else struct.pack("<I", value)
    out += struct.pack("<I", 0)
    out += struct.pack(f"<{len(offsets)}I", *offsets)
    for packed in blocks:
        out += packed
    return bytes(out)


def _khoa(points: list[DepthPoint], *, code: str = "00") -> dict[str, Any]:
    items = [{"lat": point.lat, "lot": point.lon, "dpwt": point.depth} for point in points]
    return {
        "header": {"resultCode": code, "resultMsg": "NORMAL_SERVICE"},
        "body": {"items": {"item": items}, "totalCount": len(items), "pageNo": 1, "numOfRows": 300},
    }


def _lattice(lat: float, lon: float, *, spacing: float, half_count: int, depth: float = 20) -> list[DepthPoint]:
    """Soundings `spacing` metres apart in a square around the point."""
    return [
        DepthPoint(lat + i * spacing / 110540, lon + j * spacing / (111320 * math.cos(math.radians(lat))), depth)
        for i in range(-half_count, half_count + 1)
        for j in range(-half_count, half_count + 1)
    ]


def _terrarium_png(elevation: float) -> bytes:
    """A 256-pixel Terrarium tile, every pixel at `elevation`."""
    value = elevation + 32768
    red, rest = divmod(value, 256)
    green = math.floor(rest)
    blue = round((rest - green) * 256)
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), (int(red), green, blue)).save(buffer, format="PNG")
    return buffer.getvalue()


def _pb_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pb_field(number: int, payload: bytes) -> bytes:
    return _pb_varint(number << 3 | 2) + _pb_varint(len(payload)) + payload


def _pb_varint_field(number: int, value: int) -> bytes:
    return _pb_varint(number << 3) + _pb_varint(value)


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def _mvt(layers: dict[str, list[tuple[int, dict[str, str], list[list[tuple[int, int]]]]]], extent: int = 4096) -> bytes:
    """A Mapbox Vector Tile: each layer's features as `(type, properties, rings)`, polygon rings
    written open and closed with `ClosePath`."""
    tile = b""
    for name, features in layers.items():
        keys: list[str] = []
        values: list[str] = []
        body = _pb_field(1, name.encode())
        for kind, properties, rings in features:
            tags: list[int] = []
            for key, value in properties.items():
                if key not in keys:
                    keys.append(key)
                if value not in values:
                    values.append(value)
                tags += [keys.index(key), values.index(value)]
            commands: list[int] = []
            x = y = 0
            for ring in rings:
                commands.append(1 | 1 << 3)  # MoveTo, one point
                commands += [_zigzag(ring[0][0] - x), _zigzag(ring[0][1] - y)]
                x, y = ring[0]
                commands.append(2 | (len(ring) - 1) << 3)  # LineTo, the rest
                for px, py in ring[1:]:
                    commands += [_zigzag(px - x), _zigzag(py - y)]
                    x, y = px, py
                commands.append(7 | 1 << 3)  # ClosePath
            feature = (
                _pb_field(2, b"".join(_pb_varint(t) for t in tags))
                + _pb_varint_field(3, kind)
                + _pb_field(4, b"".join(_pb_varint(c) for c in commands))
            )
            body += _pb_field(2, feature)
        body += b"".join(_pb_field(3, key.encode()) for key in keys)
        body += b"".join(_pb_field(4, _pb_field(1, value.encode())) for value in values)
        body += _pb_varint_field(5, extent) + _pb_varint_field(15, 2)
        tile += _pb_field(3, body)
    return tile


# A whole tile of sea with a square island in its middle, drawn as a hole, as OpenMapTiles
# draws islands: the outer ring clockwise, the hole the other way.
_SEA_WITH_ISLAND = [
    [(0, 0), (4096, 0), (4096, 4096), (0, 4096)],
    [(1536, 1536), (1536, 2560), (2560, 2560), (2560, 1536)],
]


# --- The fake network -----------------------------------------------------------------------


class _Network:
    """Stands in for every third party, answering each by host, and records what was asked.

    Patches the client construction in `core.utils.outbound`, so the URLs, parameters and
    redirects the service actually builds are under test while nothing leaves the machine. A
    host with no handler answers 500, which is how a test says "this one is down".
    """

    def __init__(self, handlers: dict[str, Callable[[httpx.Request], httpx.Response]]) -> None:
        self.handlers = handlers
        self.requests: list[httpx.Request] = []
        self._patcher: Any = None

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.handlers.get(request.url.host)
        return handler(request) if handler else httpx.Response(500)

    def hosts(self) -> set[str]:
        return {request.url.host for request in self.requests}

    def __enter__(self) -> _Network:
        def build(**kwargs: Any) -> httpx.AsyncClient:
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self._answer), **kwargs)

        self._patcher = patch("src.app.core.utils.outbound.httpx.AsyncClient", side_effect=build)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._patcher.stop()


GMRT = "www.gmrt.org"
ETOPO_HOSTS = ("coastwatch.pfeg.noaa.gov", "oceanwatch.pifsc.noaa.gov")
KHOA = "apis.data.go.kr"
OPENFREEMAP = "tiles.openfreemap.org"
TERRAIN = "s3.amazonaws.com"
NOAA = "gis.ngdc.noaa.gov"
EMODNET = "erddap.emodnet.eu"


def _text(body: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, text=body)


def _json_answer(body: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, json=body)


def _bytes(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, content=body)


def _status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(code)


# --- Fixtures -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seafloor_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`: nothing below the route touches a
    database."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def client(seafloor_app: Any) -> Generator[TestClient]:
    seafloor_app.dependency_overrides[get_current_user] = lambda: CURRENT_USER
    # The app's startup connects the cache to whatever Redis the settings name, which on a
    # developer's machine is the running stack's: no cache after it, so no fake answer is kept
    # there for a week.
    with TestClient(seafloor_app) as test_client, patch("src.app.core.utils.cache.client", None):
        yield test_client
    seafloor_app.dependency_overrides = {}


@pytest.fixture
def anonymous_client(seafloor_app: Any) -> Generator[TestClient]:
    with TestClient(seafloor_app) as test_client, patch("src.app.core.utils.cache.client", None):
        yield test_client
    seafloor_app.dependency_overrides = {}


@pytest.fixture
def fake_redis() -> Generator[FakeRedis]:
    redis = FakeRedis()
    with patch("src.app.core.utils.cache.client", redis):
        yield redis


@pytest.fixture
def no_redis() -> Generator[None]:
    with patch("src.app.core.utils.cache.client", None):
        yield


@pytest.fixture(autouse=True)
def unthrottled() -> Generator[AsyncMock]:
    with patch("src.app.api.v1.seafloor.enforce_rate_limit", new_callable=AsyncMock) as limit:
        yield limit


@pytest.fixture
def no_khoa_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "KHOA_API_KEY", None)


@pytest.fixture
def khoa_key(monkeypatch: pytest.MonkeyPatch) -> str:
    # A decoded key, as data.go.kr issues one: `+`, `/` and `=` all need encoding in a URL.
    key = "abc+def/ghi=="
    monkeypatch.setattr(settings, "KHOA_API_KEY", SecretStr(key))
    return key


def _seafloor(client: TestClient, lat: float, lon: float, **params: Any) -> Any:
    return client.get("/api/v1/seafloor", params={"lat": lat, "lon": lon, **params})


# --- Choosing between sources ---------------------------------------------------------------


class _Cell:
    def __init__(self, cell_size_meters: float, holds_place: bool = False, name: str = "") -> None:
        self.cell_size_meters = cell_size_meters
        self.holds_place = holds_place
        self.name = name


class TestOrderByCellSize:
    @staticmethod
    def _order(cells: list[float]) -> list[float]:
        return [candidate.cell_size_meters for candidate in order_by_cell_size([_Cell(cell) for cell in cells])]

    def test_keeps_the_preferred_order_when_no_source_is_more_than_twice_as_fine(self):
        # EMODnet's 115 m stays ahead of GMRT's 60 m.
        assert self._order([115, 60, 450]) == [115, 60, 450]

    def test_never_moves_anything_past_a_source_that_holds_its_place(self):
        # KHOA's 144 m survey data stays ahead of GMRT's nominal 60 m.
        ordered = order_by_cell_size([_Cell(144, True, "khoa"), _Cell(60, name="gmrt"), _Cell(450, name="etopo")])
        assert [candidate.name for candidate in ordered] == ["khoa", "gmrt", "etopo"]

    def test_moves_a_much_finer_source_ahead(self):
        assert self._order([115, 3.4, 60, 450]) == [3.4, 115, 60, 450]
        assert self._order([450, 60]) == [60, 450]


def test_cell_size_reports_the_coarser_axis():
    # 15 arc-seconds is about 460 m north-south anywhere, less east-west away from the equator.
    assert cell_size_meters(1 / 240, 1 / 240, 60) == pytest.approx(460.6, abs=0.5)


def test_known_and_wet_fractions_count_readings_among_cells_and_water_among_readings():
    grid = _grid(1, 4, [10, -2, None, 5])
    assert known_fraction(grid) == 0.75
    assert wet_fraction(grid) == pytest.approx(2 / 3)


# --- Parsers --------------------------------------------------------------------------------


class TestParseErddapGrid:
    BODY = {
        "table": {
            "columnNames": ["latitude", "longitude", "elevation"],
            "rows": [[10.0, 20.0, -5], [10.0, 20.5, -6], [10.5, 20.0, None], [10.5, 20.5, 3]],
        }
    }

    def test_lays_the_cells_out_south_first_and_flips_elevation_into_depth(self):
        grid = parse_erddap_grid(self.BODY, 10.25, 20.25)
        assert (grid.rows, grid.cols) == (2, 2)
        assert grid.depths == [5, 6, None, -3]
        assert grid.origin_lat == 10
        assert grid.cell_lon == 0.5

    def test_refuses_an_answer_with_no_grid_in_it(self):
        with pytest.raises(ValueError):
            parse_erddap_grid({"table": {"rows": [[1, 2, 3]]}}, 0, 0)
        with pytest.raises(ValueError):
            parse_erddap_grid({}, 0, 0)


class TestParseFloat32Tiff:
    BOUNDS = Bounds(north=2, south=0, east=3, west=0)

    def test_reads_the_strip_south_first_flips_elevation_and_empties_no_data(self):
        # North row first, as the file stores it.
        grid = parse_float32_tiff(_strip_tiff(3, 2, [-1, -2, math.nan, -10, 4, -3.4e38]), self.BOUNDS, 1, 1.5)
        assert grid.depths == [10, -4, None, 1, 2, None]

    def test_puts_the_origin_at_the_centre_of_the_south_west_pixel(self):
        grid = parse_float32_tiff(_strip_tiff(3, 2, [0] * 6), self.BOUNDS, 1, 1.5)
        assert (grid.origin_lat, grid.origin_lon, grid.cell_lat) == (0.5, 0.5, 1)

    def test_reads_tiles_and_ignores_their_padding(self):
        values = [float(-i) for i in range(5 * 3)]
        grid = parse_float32_tiff(_tiled_tiff(5, 3, 2, values), Bounds(north=3, south=0, east=5, west=0), 1.5, 2.5)
        # The file's last row is the grid's first.
        assert grid.depths[:5] == [10, 11, 12, 13, 14]
        assert grid.depths[10:] == [0, 1, 2, 3, 4]

    def test_refuses_anything_that_is_not_a_little_endian_tiff(self):
        with pytest.raises(ValueError, match="TIFF"):
            parse_float32_tiff(bytes(16), self.BOUNDS, 0, 0)
        with pytest.raises(ValueError, match="TIFF"):
            parse_float32_tiff(_strip_tiff(3, 2, [0] * 6)[:-8], self.BOUNDS, 0, 0)


class TestParseKhoaPage:
    def test_reads_the_soundings_and_the_total(self):
        page = parse_khoa_page(
            {
                "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                "body": {
                    "items": {
                        "item": [
                            {"lat": 33.2009, "lot": 126.4915, "dpwt": 86.8},
                            {"lat": "33.201", "lot": "126.4931", "dpwt": "87.55"},
                        ]
                    },
                    "totalCount": 677,
                },
            }
        )
        assert page.total == 677
        assert page.points == [DepthPoint(33.2009, 126.4915, 86.8), DepthPoint(33.201, 126.4931, 87.55)]

    def test_takes_a_single_sounding_that_arrives_as_an_object(self):
        page = parse_khoa_page(
            {
                "header": {"resultCode": "00"},
                "body": {"items": {"item": {"lat": 1, "lot": 2, "dpwt": 3}}, "totalCount": 1},
            }
        )
        assert page.points == [DepthPoint(1, 2, 3)]

    def test_treats_no_data_as_a_decline_and_anything_else_as_an_error(self):
        with pytest.raises(NoCoverageError):
            parse_khoa_page({"header": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}})
        with pytest.raises(ValueError, match="22 LIMITED"):
            parse_khoa_page(
                {"header": {"resultCode": "22", "resultMsg": "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"}}
            )


class TestParseEsriAscii:
    # Three columns, two rows, listed north first as the format does. -9999 is no reading.
    ASCII = (
        "ncols 3\nnrows 2\nxllcorner 126.0\nyllcorner 33.0\ncellsize 0.01\nnodata_value -9999\n"
        "-10 -20 5\n-30 -9999 -40\n"
    )

    def test_stores_rows_south_first_and_keeps_land_as_negative_depth(self):
        grid = parse_esri_ascii(self.ASCII, 33.01, 126.015)
        assert grid.depths[:3] == [30, None, 40]
        assert grid.depths[3:] == [10, 20, -5]

    def test_puts_the_origin_at_the_centre_of_the_south_west_cell(self):
        grid = parse_esri_ascii(self.ASCII, 33.01, 126.015)
        assert grid.origin_lat == pytest.approx(33.005)
        assert grid.origin_lon == pytest.approx(126.005)
        assert (grid.rows, grid.cols) == (2, 3)

    def test_refuses_a_grid_with_too_few_values(self):
        with pytest.raises(ValueError, match="expected 6"):
            parse_esri_ascii(self.ASCII.replace("-30 -9999 -40", "-30"), 33, 126)


# --- Grid operations ------------------------------------------------------------------------


class TestDownsample:
    GRID = _grid(4, 4, [1, 3, None, None, 5, 7, None, None, 2, 2, 4, 4, 2, 2, 4, None])

    def test_averages_blocks_and_ignores_empty_cells(self):
        small = downsample(self.GRID, 2)
        assert (small.rows, small.cols) == (2, 2)
        assert small.depths == [4, None, 2, 4]

    def test_moves_the_origin_to_the_first_blocks_centre_and_widens_the_cells(self):
        small = downsample(self.GRID, 2)
        assert (small.origin_lat, small.cell_lat, small.resolution_meters) == (0.5, 2, 200)

    def test_leaves_a_grid_that_already_fits_alone(self):
        assert downsample(self.GRID, 4) is self.GRID


def test_bounds_around_is_a_square_of_the_given_span():
    bounds = bounds_around(33, 126, 8000)
    assert (bounds.north - bounds.south) * 110540 == pytest.approx(8000)
    assert (bounds.east - bounds.west) * 111320 * math.cos(math.radians(33)) == pytest.approx(8000)


class TestCropToBounds:
    # Ten cells a side, one degree apart.
    GRID = _grid(10, 10, [float(i) for i in range(100)])

    def test_keeps_only_the_cells_whose_centres_fall_inside(self):
        cropped = crop_to_bounds(self.GRID, Bounds(north=5, south=3, east=6, west=4))
        assert (cropped.rows, cropped.cols, cropped.origin_lat, cropped.origin_lon) == (3, 3, 3, 4)
        assert cropped.depths[:3] == [34, 35, 36]

    def test_leaves_a_grid_that_is_no_more_than_10_percent_wider_alone(self):
        assert crop_to_bounds(self.GRID, Bounds(north=9, south=0, east=9, west=0)) is self.GRID


def test_depth_at_interpolates_and_is_none_off_the_grid_or_beside_a_gap():
    grid = _grid(2, 2, [0, 10, 20, 30])
    assert depth_at(grid, 0, 0.5) == 5
    assert depth_at(grid, 0.5, 0.5) == 15
    assert depth_at(grid, -0.1, 0) is None
    # Every corner of the cell is needed, even on its edge.
    assert depth_at(_grid(2, 2, [0, 10, 20, None]), 0, 0.5) is None


# --- Gridding soundings ---------------------------------------------------------------------


class TestGridPoints:
    # A box a little over 300 m a side at the equator, gridded every 100 m: 4 x 4 cells.
    BOUNDS = Bounds(south=0, north=0.0028, west=0, east=0.0028)

    def test_puts_a_soundings_depth_on_the_cell_it_sits_on(self):
        grid = grid_points([DepthPoint(0, 0, 12)], self.BOUNDS, 100, 150, 0, 0)
        assert grid.depths[0] == 12

    def test_weights_nearby_soundings_by_distance(self):
        grid = grid_points([DepthPoint(0, 0.0003, 10), DepthPoint(0, 0.0015, 40)], self.BOUNDS, 100, 150, 0, 0)
        # The cell 100 m east sits about 33 m from the first and 67 m from the second.
        assert 10 < grid.depths[1] < 25  # type: ignore[operator]

    def test_leaves_cells_far_from_every_sounding_empty(self):
        grid = grid_points([DepthPoint(0, 0, 12)], self.BOUNDS, 100, 150, 0, 0)
        assert grid.depths[-1] is None
        assert (grid.rows, grid.cols) == (4, 4)


class TestGridPointsWithALandRule:
    # A lattice of soundings 144 m apart, 20 m deep, with the middle one missing: a rock.
    POINTS = [
        DepthPoint(i * 144 / 110540, j * 144 / 111320, 20)
        for i in range(-2, 3)
        for j in range(-2, 3)
        if (i, j) != (0, 0)
    ]
    HALF = 2 * 144 / 110540
    BOUNDS = Bounds(south=-HALF, north=HALF, west=-HALF, east=HALF)

    def _at(self, grid: Grid, north: float, east: float) -> float | None:
        r = round((north + 2 * 144) / 72)
        c = round((east + 2 * 144) / 72)
        return grid.depths[r * grid.cols + c]

    def test_makes_the_spot_of_a_missing_sounding_land_even_a_single_node(self):
        grid = grid_points(self.POINTS, self.BOUNDS, 72, 216, 0, 0, LandRule(depth=-3, beyond_meters=115))
        assert self._at(grid, 0, 0) == -3

    def test_keeps_open_water_between_soundings_as_water(self):
        grid = grid_points(self.POINTS, self.BOUNDS, 72, 216, 0, 0, LandRule(depth=-3, beyond_meters=115))
        # Halfway between two soundings, and at the middle of a lattice square.
        assert self._at(grid, 144, 72) == pytest.approx(20)
        assert self._at(grid, -216, -216) == pytest.approx(20)


class TestGridPointsWithACoastline:
    # Soundings 20 m deep on a 144 m lattice, all present: no gap marks any land.
    POINTS = [DepthPoint(i * 144 / 110540, j * 144 / 111320, 20) for i in range(-2, 3) for j in range(-2, 3)]
    HALF = 288 / 110540
    BOUNDS = Bounds(south=-HALF, north=HALF, west=-HALF, east=HALF)

    @staticmethod
    def _is_water(lat: float, lon: float) -> bool:
        # The map puts a small island just north-east of the middle, nowhere near a gap.
        return not (0 < lat < 40 / 110540 and 0 < lon < 40 / 111320)

    def test_draws_the_maps_island_even_where_every_sounding_is_present(self):
        grid = grid_points(
            self.POINTS, self.BOUNDS, 36, 216, 0, 0, LandRule(depth=-3, beyond_meters=115, is_water=self._is_water)
        )
        assert grid.land_from == "openstreetmap"
        assert grid.depths.count(-3) > 0
        # The south-west corner is open water.
        assert grid.depths[0] == pytest.approx(20)

    def test_falls_back_to_the_gaps_between_soundings_without_a_coastline(self):
        grid = grid_points(self.POINTS, self.BOUNDS, 36, 216, 0, 0, LandRule(depth=-3, beyond_meters=115))
        assert grid.land_from == "soundings"
        assert -3 not in grid.depths


def test_grid_points_asks_the_height_function_for_each_land_cell():
    half = 200 / 110540
    grid = grid_points(
        [DepthPoint(0, 0, 20)],
        Bounds(south=-half, north=half, west=-half, east=half),
        100,
        150,
        0,
        0,
        # Land rising to the east: 1 m higher per 0.001 degree of longitude.
        LandRule(depth=lambda _lat, lon: -(5 + lon * 1000), beyond_meters=115),
    )
    west = grid.depths[0]
    east = grid.depths[grid.cols - 1]
    assert west is not None and east is not None
    assert west < 0
    assert east < west


def _naive_grid_points(
    points: list[DepthPoint],
    bounds: Bounds,
    cell_meters: float,
    radius_meters: float,
    center_lat: float,
    land: LandRule | None,
) -> list[float | None]:
    """`grid_points` as the web app wrote it: every cell against every point."""
    per_lon = 111320 * math.cos(math.radians(center_lat))
    cell_lat = cell_meters / 110540
    cell_lon = cell_meters / per_lon
    rows = max(2, math.floor((bounds.north - bounds.south) / cell_lat) + 1)
    cols = max(2, math.floor((bounds.east - bounds.west) / cell_lon) + 1)
    local = [((p.lon - bounds.west) * per_lon, (p.lat - bounds.south) * 110540, p.depth) for p in points]
    samples = (-1 / 3, 0, 1 / 3)

    def coast(lat: float, lon: float) -> bool | None:
        if land is None or land.is_water is None:
            return None
        known = False
        for dy in samples:
            for dx in samples:
                water = land.is_water(lat + dy * cell_lat, lon + dx * cell_lon)
                if water is False:
                    return False
                if water is True:
                    known = True
        return True if known else None

    depths: list[float | None] = []
    for r in range(rows):
        for c in range(cols):
            x, y = c * cell_meters, r * cell_meters
            weights = total = 0.0
            exact = None
            nearest_squared, nearest_depth = math.inf, None
            for px, py, depth in local:
                d2 = (px - x) * (px - x) + (py - y) * (py - y)
                if d2 < nearest_squared:
                    nearest_squared, nearest_depth = d2, depth
                if d2 > radius_meters * radius_meters:
                    continue
                if d2 < 1:
                    exact = depth
                    continue
                weights += 1 / d2
                total += 1 / d2 * depth
            weighted = exact if exact is not None else (total / weights if weights > 0 else None)
            lat, lon = bounds.south + r * cell_lat, bounds.west + c * cell_lon
            water = coast(lat, lon)
            land_depth = (lambda: land.depth(lat, lon) if callable(land.depth) else land.depth) if land else None
            if water is False and land_depth:
                depths.append(land_depth())
            elif water is True:
                depths.append(weighted if weighted is not None else nearest_depth)
            elif land_depth and nearest_squared > land.beyond_meters**2:  # type: ignore[union-attr]
                depths.append(land_depth())
            else:
                depths.append(weighted)
    return depths


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_grid_points_gives_exactly_what_weighing_every_point_gives(seed: int):
    """The buckets are a speed-up and nothing else: same cells, same depths, to the bit."""
    rng = random.Random(seed)
    lat = 33.2
    bounds = bounds_around(lat, 126.5, 1200)
    # Soundings scattered past the box, as KHOA's wider fetch scatters them, with a bare patch.
    points = [
        DepthPoint(
            rng.uniform(bounds.south - 0.002, bounds.north + 0.002),
            rng.uniform(bounds.west - 0.002, bounds.east + 0.002),
            rng.uniform(1, 60),
        )
        for _ in range(120)
    ]
    points = [p for p in points if not (33.199 < p.lat < 33.202 and 126.498 < p.lon < 126.503)]

    def is_water(la: float, lo: float) -> bool | None:
        if la > 33.204:
            return None  # off the map's tiles
        return (la - 33.2) ** 2 + (lo - 126.5) ** 2 > 0.0012**2

    rules = [
        None,
        LandRule(depth=-4, beyond_meters=115),
        LandRule(depth=lambda la, lo: -(la + lo), beyond_meters=300, is_water=is_water),
    ]
    for rule in rules:
        radius = 216 if rule is None or rule.beyond_meters < 216 else 150
        fast = grid_points(points, bounds, 40, radius, lat, 126.5, rule)
        assert fast.depths == _naive_grid_points(points, bounds, 40, radius, lat, rule)


# --- Map tiles ------------------------------------------------------------------------------


def test_puts_a_point_in_the_right_web_mercator_tile():
    # Munseom, off Seogwipo, at zoom 14: tile 13952 / 6587.
    x, y = tile_position(33.22769, 126.56571, 14)
    assert (math.floor(x), math.floor(y)) == (13952, 6587)


def test_lists_every_tile_a_box_touches():
    tiles = tiles_covering(Bounds(north=33.2367, south=33.2187, east=126.5765, west=126.555), 14)
    assert 1 <= len(tiles) <= 4


class TestWaterTest:
    # One tile at zoom 1 (x 1, y 0: the north-east quarter of the world), all sea except a
    # square island in its middle, drawn as a hole.
    TEST = staticmethod(
        water_test(
            [
                WaterTile(
                    z=1,
                    x=1,
                    y=0,
                    extent=100,
                    polygons=[
                        TilePolygon(
                            rings=[[(0, 0), (100, 0), (100, 100), (0, 100)], [(40, 40), (40, 60), (60, 60), (60, 40)]]
                        )
                    ],
                )
            ]
        )
    )

    def test_is_water_in_the_sea_land_in_the_island_and_unknown_off_the_tile(self):
        assert self.TEST(60, 30) is True
        # The tile's middle, at x 50 / y 50 of 100.
        assert self.TEST(66.51326, 90) is False
        assert self.TEST(10, -30) is None

    def test_gives_exactly_what_the_even_odd_ray_cast_gives(self):
        """The crossing rows are a speed-up and nothing else, holes and concave rings included."""
        rng = random.Random(7)
        extent = 4096

        def star(cx: float, cy: float, radius: float, points: int) -> list[tuple[float, float]]:
            return [
                (
                    cx + math.cos(2 * math.pi * k / points) * radius * rng.uniform(0.4, 1),
                    cy + math.sin(2 * math.pi * k / points) * radius * rng.uniform(0.4, 1),
                )
                for k in range(points)
            ]

        polygons = [
            TilePolygon(rings=[star(2048, 2048, 2000, 60), star(2048, 2048, 500, 20)]),
            TilePolygon(rings=[star(600, 3500, 400, 15)]),
        ]
        tile = WaterTile(z=14, x=13952, y=6587, extent=extent, polygons=polygons)
        test = water_test([tile])

        def naive(px: float, py: float) -> bool:
            for polygon in polygons:
                inside = False
                for ring in polygon.rings:
                    j = len(ring) - 1
                    for i in range(len(ring)):
                        (ax, ay), (bx, by) = ring[i], ring[j]
                        if (ay > py) != (by > py) and px < (bx - ax) * (py - ay) / (by - ay) + ax:
                            inside = not inside
                        j = i
                if inside:
                    return True
            return False

        n = 2**14
        for _ in range(3000):
            px, py = rng.uniform(0, extent), rng.uniform(0, extent)
            lon = (13952 + px / extent) / n * 360 - 180
            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (6587 + py / extent) / n))))
            x, y = tile_position(lat, lon, 14)
            assert test(lat, lon) == naive((x - 13952) * extent, (y - 6587) * extent)


def test_terrarium_decodes_red_green_and_blue_into_metres():
    assert terrarium_elevation(128, 0, 0) == 0
    assert terrarium_elevation(128, 73, 76.8) == pytest.approx(73.3)  # type: ignore[arg-type]
    assert terrarium_elevation(127, 255, 0) == -1


class TestTerrainSampler:
    # One 2 x 2 zoom-0 tile (the whole world), rising from 0 m in the west to 100 m in the east.
    SAMPLE = staticmethod(terrain_sampler([TerrainTile(x=0, y=0, size=2, elevations=[0, 100, 0, 100])], 0))

    def test_interpolates_between_pixel_centres(self):
        # Longitude 0 is halfway between the two pixel centres (at -90 and 90 degrees).
        assert self.SAMPLE(0, 0) == pytest.approx(50)

    def test_is_none_where_a_pixel_it_needs_is_on_no_tile(self):
        assert self.SAMPLE(0, 179) is None


# --- Vector tiles ---------------------------------------------------------------------------


class TestVectorTiles:
    TILE = _mvt(
        {
            "landcover": [(3, {"class": "grass"}, [[(0, 0), (10, 0), (10, 10)]])],
            "water": [
                (3, {"class": "ocean"}, _SEA_WITH_ISLAND),
                (3, {"class": "lake"}, [[(100, 100), (200, 100), (200, 200)]]),
                (2, {"class": "ocean"}, [[(0, 0), (5, 5)]]),
            ],
        }
    )

    def test_reads_the_named_layer_and_nothing_else(self):
        layer = vector_tiles.read_layer(self.TILE, "water")
        assert layer is not None
        assert layer.extent == 4096
        assert [(feature.type, feature.properties["class"]) for feature in layer.features] == [
            (3, "ocean"),
            (3, "lake"),
            (2, "ocean"),
        ]
        assert vector_tiles.read_layer(self.TILE, "transportation") is None

    def test_closes_each_ring_by_repeating_its_first_point(self):
        layer = vector_tiles.read_layer(self.TILE, "water")
        assert layer is not None
        rings = layer.features[0].rings()
        assert rings[0] == [(0, 0), (4096, 0), (4096, 4096), (0, 4096), (0, 0)]
        assert rings[1][0] == rings[1][-1] == (1536, 1536)

    def test_keeps_the_sea_and_drops_lakes_and_lines(self):
        tile = seafloor._water_tile(self.TILE, 3, 4)
        assert (tile.x, tile.y, tile.extent) == (3, 4, 4096)
        assert len(tile.polygons) == 1

    def test_reads_a_tile_still_gzipped(self):
        assert len(seafloor._water_tile(gzip.compress(self.TILE), 3, 4).polygons) == 1

    def test_a_tile_with_no_water_layer_is_all_land(self):
        tile = seafloor._water_tile(_mvt({"landcover": []}), 0, 0)
        assert tile.polygons == []

    def test_refuses_a_truncated_tile(self):
        with pytest.raises(ValueError):
            vector_tiles.read_layer(self.TILE[:-5], "water")


# --- Resolving a point ----------------------------------------------------------------------


@pytest.mark.usefixtures("no_redis", "no_khoa_key")
class TestResolve:
    @pytest.mark.asyncio
    async def test_open_ocean_is_answered_by_gmrt_without_asking_anything_else(self):
        with _Network({GMRT: _text(_esri_ascii(*OPEN_PACIFIC))}) as network:
            result = await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)

        assert (result.status, result.source) == ("ok", "gmrt")
        assert [(a.source, a.outcome) for a in result.attempts] == [
            ("noaa_dem", "no_coverage"),
            ("emodnet", "no_coverage"),
            ("khoa", "no_coverage"),
            ("gmrt", "used"),
            ("etopo2022", "not_needed"),
        ]
        assert network.hosts() == {GMRT}
        used = result.attempts[3]
        assert (used.cell_size_meters, used.cols, used.rows, used.known_fraction, used.wet_fraction) == (
            60,
            10,
            10,
            1,
            1,
        )
        assert result.grid is not None
        assert (result.grid.cols, result.grid.rows) == (10, 10)
        assert result.grid.depths == [4000] * 100
        assert (result.grid.land_from, result.grid.land_height_from) == (None, None)

    @pytest.mark.asyncio
    async def test_gmrt_is_asked_for_its_finest_grid_over_the_box(self):
        with _Network({GMRT: _text(_esri_ascii(*OPEN_PACIFIC))}) as network:
            await seafloor.resolve_seafloor(*OPEN_PACIFIC, 1000)

        params = network.requests[0].url.params
        bounds = bounds_around(*OPEN_PACIFIC, 1000)
        assert params["format"] == "esriascii"
        assert params["resolution"] == "max"
        assert params["north"] == f"{bounds.north:.6f}"

    @pytest.mark.asyncio
    async def test_a_failed_source_is_recorded_and_the_next_one_tried(self):
        etopo = _erddap(*OPEN_PACIFIC, cells=12, cell=0.0015)
        with _Network({GMRT: _status(502), ETOPO_HOSTS[0]: _json_answer(etopo)}):
            result = await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)

        assert (result.status, result.source) == ("ok", "etopo2022")
        gmrt = result.attempts[3]
        assert (gmrt.source, gmrt.outcome, gmrt.error) == ("gmrt", "failed", "HTTP 502")

    @pytest.mark.asyncio
    async def test_etopo_falls_back_to_its_second_host(self):
        etopo = _erddap(*OPEN_PACIFIC, cells=12, cell=0.0015)
        with _Network({GMRT: _status(500), ETOPO_HOSTS[1]: _json_answer(etopo)}) as network:
            result = await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)

        assert result.source == "etopo2022"
        assert {ETOPO_HOSTS[0], ETOPO_HOSTS[1]} <= network.hosts()

    @pytest.mark.asyncio
    async def test_etopo_is_asked_for_ten_kilometres_with_the_subset_in_the_query(self):
        with _Network({}) as network:
            await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)

        etopo = next(r for r in network.requests if r.url.host == ETOPO_HOSTS[0])
        wide = bounds_around(*OPEN_PACIFIC, 10_000)
        assert etopo.url.path.endswith("/ETOPO_2022_v1_15s.json")
        assert etopo.url.query.decode() == f"z%5B({wide.south}):({wide.north})%5D%5B({wide.west}):({wide.east})%5D"

    @pytest.mark.asyncio
    async def test_a_point_on_land_is_dry(self):
        # GMRT knows the land, all of it above water; ETOPO's coarse cells are too few at 2 km.
        land = _esri_ascii(*OPEN_PACIFIC, elevation=120)
        coarse = _erddap(*OPEN_PACIFIC, cells=24, cell=1 / 240, elevation=150)
        with _Network({GMRT: _text(land), ETOPO_HOSTS[0]: _json_answer(coarse)}):
            result = await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)

        assert (result.status, result.source, result.grid) == ("dry", None, None)
        gmrt, etopo = result.attempts[3:]
        assert (gmrt.outcome, gmrt.known_fraction, gmrt.wet_fraction) == ("mostly_land", 1, 0)
        assert (etopo.outcome, etopo.cols, etopo.rows) == ("too_coarse", 4, 4)

    @pytest.mark.asyncio
    async def test_land_with_a_source_down_is_a_failure_not_dry(self):
        with _Network({GMRT: _text(_esri_ascii(*OPEN_PACIFIC, elevation=120))}):
            result = await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)

        assert result.status == "failed"
        assert [a.outcome for a in result.attempts[3:]] == ["mostly_land", "failed"]

    @pytest.mark.asyncio
    async def test_patchy_coverage_is_too_little_data(self):
        # The northern half of the rows has no reading.
        lines = _esri_ascii(*OPEN_PACIFIC).splitlines()
        patchy = "\n".join(lines[:6] + [" ".join(["-99999"] * 10)] * 5 + lines[11:])
        with _Network({GMRT: _text(patchy)}):
            result = await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)

        gmrt = result.attempts[3]
        assert (gmrt.outcome, gmrt.known_fraction) == ("too_little_data", 0.5)

    @pytest.mark.asyncio
    async def test_an_unreadable_answer_is_a_failure_with_its_reason(self):
        with _Network({GMRT: _text("<html>busy</html>")}):
            result = await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)

        assert result.attempts[3].outcome == "failed"
        assert result.attempts[3].error == 'ESRI ASCII grid is missing "ncols"'


@pytest.mark.usefixtures("no_redis")
class TestNoaa:
    SAN_DIEGO = (32.85, -117.27)

    @staticmethod
    def _identify(low_ps: float) -> Callable[[httpx.Request], httpx.Response]:
        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/identify"):
                return httpx.Response(
                    200,
                    json={
                        "catalogItems": {
                            "features": [{"attributes": {"LowPS": low_ps}}, {"attributes": {"LowPS": 0.004}}]
                        }
                    },
                )
            values = [-30.0] * (160 * 160)
            return httpx.Response(200, content=_strip_tiff(160, 160, values))

        return answer

    @pytest.mark.asyncio
    async def test_a_fine_dem_is_asked_for_160_pixels_and_used(self, no_khoa_key: None):
        with _Network({NOAA: self._identify(0.0000925)}) as network:
            result = await seafloor.resolve_seafloor(*self.SAN_DIEGO, 2000)

        assert (result.status, result.source) == ("ok", "noaa_dem")
        assert result.grid is not None and (result.grid.cols, result.grid.rows) == (160, 160)
        # The finest item's 0.0000925 degrees, north-south.
        assert result.attempts[2].cell_size_meters == pytest.approx(10.22, abs=0.01)
        export = network.requests[1]
        assert export.url.path.endswith("/exportImage")
        assert (export.url.params["size"], export.url.params["pixelType"], export.url.params["f"]) == (
            "160,160",
            "F32",
            "image",
        )
        identify = network.requests[0].url.params
        assert identify["geometry"] == '{"x":-117.27,"y":32.85}'

    @pytest.mark.asyncio
    async def test_only_the_etopo_background_is_no_coverage(self, no_khoa_key: None):
        with _Network({NOAA: self._identify(0.004), GMRT: _text(_esri_ascii(*self.SAN_DIEGO))}):
            result = await seafloor.resolve_seafloor(*self.SAN_DIEGO, 2000)

        assert result.attempts[0].model_dump(exclude_none=True) == {"source": "noaa_dem", "outcome": "no_coverage"}
        assert result.source == "gmrt"

    @pytest.mark.asyncio
    async def test_a_probe_that_cannot_be_answered_is_a_failure(self, no_khoa_key: None):
        with _Network({NOAA: _status(503), GMRT: _text(_esri_ascii(*self.SAN_DIEGO))}):
            result = await seafloor.resolve_seafloor(*self.SAN_DIEGO, 2000)

        noaa = next(a for a in result.attempts if a.source == "noaa_dem")
        assert (noaa.outcome, noaa.error, noaa.cell_size_meters) == ("failed", "HTTP 503", None)
        assert result.source == "gmrt"


@pytest.mark.usefixtures("no_redis")
class TestEmodnet:
    MARSEILLE = (43.21, 5.33)

    @pytest.mark.asyncio
    async def test_european_seas_are_asked_of_the_2024_dtm(self, no_khoa_key: None):
        body = _erddap(*self.MARSEILLE, cells=18, cell=1 / 960, elevation=-60)
        with _Network({EMODNET: _json_answer(body)}) as network:
            result = await seafloor.resolve_seafloor(*self.MARSEILLE, 2000)

        assert (result.status, result.source) == ("ok", "emodnet")
        assert network.requests[0].url.path == "/erddap/griddap/bathymetry_dtm_2024.json"
        assert network.requests[0].url.query.decode().startswith("elevation%5B(")
        assert result.attempts[1].model_dump(exclude_none=True) == {"source": "khoa", "outcome": "no_coverage"}


@pytest.mark.usefixtures("no_redis")
class TestKhoa:
    @pytest.mark.asyncio
    async def test_without_a_key_it_is_not_configured_and_the_global_sources_answer(self, no_khoa_key: None):
        with _Network({GMRT: _text(_esri_ascii(*JEJU))}) as network:
            result = await seafloor.resolve_seafloor(*JEJU, 2000)

        assert result.attempts[2].model_dump(exclude_none=True) == {"source": "khoa", "outcome": "not_configured"}
        assert result.source == "gmrt"
        assert KHOA not in network.hosts()

    @pytest.mark.asyncio
    async def test_soundings_without_a_coastline_or_terrain_are_land_by_their_gaps_drawn_flat(self, khoa_key: str):
        # Soundings 250 m apart: the middle of each lattice square is farther than 115 m from
        # all four of its corners, so it is land.
        soundings = _lattice(*JEJU, spacing=250, half_count=5)
        with _Network({KHOA: _json_answer(_khoa(soundings))}) as network:
            result = await seafloor.resolve_seafloor(*JEJU, 2000)

        assert (result.status, result.source) == ("ok", "khoa")
        assert result.grid is not None
        assert (result.grid.land_from, result.grid.land_height_from) == ("soundings", "flat")
        assert (result.grid.cols, result.grid.rows) == (125, 126)
        # A tenth of the deepest sounding, at least 2 m.
        assert -2 in result.grid.depths
        assert result.attempts[2].known_fraction == 1
        # GMRT was asked only for land heights, which failed; it was never tried as a source.
        assert [(a.source, a.outcome) for a in result.attempts[3:]] == [
            ("gmrt", "not_needed"),
            ("etopo2022", "not_needed"),
        ]
        assert {OPENFREEMAP, TERRAIN, GMRT} <= network.hosts()

    @pytest.mark.asyncio
    async def test_the_key_is_sent_encoded_once_and_the_box_to_five_decimals(self, khoa_key: str):
        with _Network({KHOA: _json_answer(_khoa(_lattice(*JEJU, spacing=250, half_count=5)))}) as network:
            await seafloor.resolve_seafloor(*JEJU, 2000)

        request = next(r for r in network.requests if r.url.host == KHOA)
        query = request.url.query.decode()
        assert "serviceKey=abc%2Bdef%2Fghi%3D%3D&" in query
        params = request.url.params
        assert params["serviceKey"] == khoa_key
        assert all(len(params[name].split(".")[1]) == 5 for name in ("ymin", "ymax", "xmin", "xmax"))
        # The box reaches 180 m past the view on every side.
        view = bounds_around(*JEJU, 2000)
        assert float(params["ymax"]) == pytest.approx(view.north + 180 / 110540, abs=1e-5)
        assert (params["pageNo"], params["numOfRows"], params["type"]) == ("1", "300", "json")

    @pytest.mark.asyncio
    async def test_an_already_encoded_key_is_not_encoded_again(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "KHOA_API_KEY", SecretStr(" abc%2Bdef "))
        with _Network({KHOA: _json_answer(_khoa(_lattice(*JEJU, spacing=250, half_count=5)))}) as network:
            await seafloor.resolve_seafloor(*JEJU, 2000)

        request = next(r for r in network.requests if r.url.host == KHOA)
        assert "serviceKey=abc%2Bdef&" in request.url.query.decode()

    @pytest.mark.asyncio
    async def test_the_coastline_and_terrain_shape_the_land(self, khoa_key: str):
        """Every zoom-14 tile is sea with an island in its middle, and the terrain stands 50 m
        tall: the island's cells are land at 50 m, the rest water from the soundings."""
        coast = _mvt({"water": [(3, {"class": "ocean"}, _SEA_WITH_ISLAND)]})
        handlers = {
            KHOA: _json_answer(_khoa(_lattice(*JEJU, spacing=144, half_count=8))),
            OPENFREEMAP: lambda request: (
                httpx.Response(200, json={"tiles": ["https://tiles.openfreemap.org/planet/1/{z}/{x}/{y}.pbf"]})
                if request.url.path == "/planet"
                else httpx.Response(200, content=coast)
            ),
            TERRAIN: _bytes(_terrarium_png(50)),
        }
        with _Network(handlers) as network:
            result = await seafloor.resolve_seafloor(*JEJU, 2000)

        assert result.grid is not None
        assert (result.grid.land_from, result.grid.land_height_from) == ("openstreetmap", "aws_terrain")
        assert -50 in result.grid.depths
        assert 20 in result.grid.depths
        assert {d for d in result.grid.depths if d is not None} <= {-50.0, 20.0}
        tiles = [r.url.path for r in network.requests if r.url.host == OPENFREEMAP and r.url.path != "/planet"]
        assert tiles and all(path.startswith("/planet/1/14/") for path in tiles)

    @pytest.mark.asyncio
    async def test_gmrt_stands_in_for_the_terrain(self, khoa_key: str):
        handlers = {
            KHOA: _json_answer(_khoa(_lattice(*JEJU, spacing=250, half_count=5))),
            GMRT: _text(_esri_ascii(*JEJU, cells=20, cell=0.002, elevation=30)),
        }
        with _Network(handlers):
            result = await seafloor.resolve_seafloor(*JEJU, 2000)

        assert result.grid is not None
        assert result.grid.land_height_from == "gmrt"
        assert -30 in result.grid.depths

    @pytest.mark.asyncio
    async def test_every_page_is_read_up_to_the_cap(self, khoa_key: str):
        soundings = _lattice(*JEJU, spacing=144, half_count=8)

        def pages(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["pageNo"])
            body = _khoa(soundings[(page - 1) * 100 : page * 100])
            body["body"]["totalCount"] = 5000
            return httpx.Response(200, json=body)

        with _Network({KHOA: pages}) as network:
            result = await seafloor.resolve_seafloor(*JEJU, 2000)

        asked = sorted(int(r.url.params["pageNo"]) for r in network.requests if r.url.host == KHOA)
        assert asked == list(range(1, 11))
        assert result.source == "khoa"

    @pytest.mark.asyncio
    async def test_no_soundings_is_no_coverage(self, khoa_key: str):
        with _Network(
            {
                KHOA: _json_answer({"header": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}}),
                GMRT: _text(_esri_ascii(*JEJU)),
            }
        ):
            result = await seafloor.resolve_seafloor(*JEJU, 2000)

        assert (result.attempts[2].outcome, result.attempts[2].cell_size_meters) == ("no_coverage", 144)
        assert result.source == "gmrt"

    @pytest.mark.asyncio
    async def test_an_xml_refusal_is_a_failure_in_the_portals_words(
        self, khoa_key: str, caplog: pytest.LogCaptureFixture
    ):
        refusal = (
            "<OpenAPI_ServiceResponse><cmmMsgHeader><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
            "</returnAuthMsg></cmmMsgHeader></OpenAPI_ServiceResponse>"
        )
        with caplog.at_level(logging.INFO), _Network({KHOA: _text(refusal), GMRT: _text(_esri_ascii(*JEJU))}):
            result = await seafloor.resolve_seafloor(*JEJU, 2000)

        khoa = result.attempts[2]
        assert (khoa.outcome, khoa.error) == ("failed", "KHOA SERVICE_KEY_IS_NOT_REGISTERED_ERROR")
        assert result.source == "gmrt"
        assert "abc+def" not in caplog.text
        assert "abc%2Bdef" not in caplog.text


class TestTheCache:
    @pytest.mark.asyncio
    async def test_an_answer_is_kept_for_a_week_and_served_without_asking(
        self, fake_redis: FakeRedis, no_khoa_key: None
    ):
        with _Network({GMRT: _text(_esri_ascii(*OPEN_PACIFIC))}):
            first = await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)
        with _Network({}) as network:
            second = await seafloor.resolve_seafloor(10.00001, -150.00002, 2000)

        assert network.requests == []
        assert second == first
        [key] = fake_redis.writes
        assert key == "seafloor:v1:nokhoa:2000:10.0:-150.0"
        assert fake_redis.expiries[key] == 7 * 24 * 60 * 60

    @pytest.mark.asyncio
    async def test_a_failure_is_not_kept(self, fake_redis: FakeRedis, no_khoa_key: None):
        with _Network({}):
            result = await seafloor.resolve_seafloor(*OPEN_PACIFIC, 2000)

        assert result.status == "failed"
        assert fake_redis.writes == []

    @pytest.mark.asyncio
    async def test_the_key_says_whether_khoa_was_configured(self, fake_redis: FakeRedis, khoa_key: str):
        with _Network({GMRT: _text(_esri_ascii(*OPEN_PACIFIC, cell=0.001))}):
            await seafloor.resolve_seafloor(*OPEN_PACIFIC, 1000)

        assert fake_redis.writes == ["seafloor:v1:khoa:1000:10.0:-150.0"]


# --- The route ------------------------------------------------------------------------------


@pytest.mark.usefixtures("no_redis", "no_khoa_key")
class TestRoute:
    def test_answers_the_grid(self, client: TestClient):
        with _Network({GMRT: _text(_esri_ascii(*OPEN_PACIFIC))}):
            response = _seafloor(client, *OPEN_PACIFIC)

        assert response.status_code == 200
        body = response.json()
        assert (body["status"], body["source"]) == ("ok", "gmrt")
        assert set(body["grid"]) == {
            "origin_lat",
            "origin_lon",
            "cell_lat",
            "cell_lon",
            "rows",
            "cols",
            "depths",
            "resolution_meters",
            "center_lat",
            "center_lon",
            "land_from",
            "land_height_from",
        }
        assert body["attempts"][3] == {
            "source": "gmrt",
            "outcome": "used",
            "cell_size_meters": 60.0,
            "cols": 10,
            "rows": 10,
            # GMRT's cell is square in degrees: 0.002 of longitude at 10 degrees north.
            "resolution_meters": pytest.approx(0.002 * 111320 * math.cos(math.radians(10))),
            "known_fraction": 1.0,
            "wet_fraction": 1.0,
            "error": None,
        }

    def test_rounds_the_point_to_four_decimals(self, client: TestClient):
        with _Network({GMRT: _text(_esri_ascii(10.0001, -150.0001))}):
            body = _seafloor(client, 10.00005, -150.00005).json()

        assert (body["grid"]["center_lat"], body["grid"]["center_lon"]) == (10.0001, -150.0)

    def test_a_dry_point_is_200(self, client: TestClient):
        land = _esri_ascii(*OPEN_PACIFIC, elevation=120)
        coarse = _erddap(*OPEN_PACIFIC, cells=24, cell=1 / 240, elevation=150)
        with _Network({GMRT: _text(land), ETOPO_HOSTS[0]: _json_answer(coarse)}):
            response = _seafloor(client, *OPEN_PACIFIC)

        assert response.status_code == 200
        assert response.json()["status"] == "dry"
        assert response.json()["grid"] is None

    def test_a_failure_is_503_and_still_carries_the_attempts(self, client: TestClient):
        with _Network({}):
            response = _seafloor(client, *OPEN_PACIFIC)

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "failed"
        assert body["detail"] == "No seafloor data could be fetched for this point. Try again later."
        assert [(a["source"], a["outcome"], a["error"]) for a in body["attempts"][3:]] == [
            ("gmrt", "failed", "HTTP 500"),
            ("etopo2022", "failed", "HTTP 500"),
        ]

    def test_a_narrow_span_is_asked_for(self, client: TestClient):
        with _Network({GMRT: _text(_esri_ascii(*OPEN_PACIFIC, cell=0.001))}) as network:
            response = _seafloor(client, *OPEN_PACIFIC, span=1000)

        assert response.status_code == 200
        bounds = bounds_around(*OPEN_PACIFIC, 1000)
        assert network.requests[0].url.params["south"] == f"{bounds.south:.6f}"

    @pytest.mark.parametrize(
        "params",
        [
            {"lat": 86, "lon": 0},
            {"lat": 0, "lon": 181},
            {"lat": 0, "lon": 0, "span": 1500},
            {"lat": "north", "lon": 0},
            {"lon": 0},
        ],
    )
    def test_refuses_what_it_cannot_answer(self, client: TestClient, params: dict[str, Any]):
        assert client.get("/api/v1/seafloor", params=params).status_code == 422

    def test_requires_authentication(self, anonymous_client: TestClient):
        assert _seafloor(anonymous_client, *OPEN_PACIFIC).status_code == 401

    def test_is_rate_limited_per_account(self, client: TestClient, unthrottled: AsyncMock):
        with _Network({GMRT: _text(_esri_ascii(*OPEN_PACIFIC))}):
            _seafloor(client, *OPEN_PACIFIC)

        unthrottled.assert_awaited_once_with(
            "seafloor:user:7", settings.SEAFLOOR_RATE_LIMIT_PER_USER, settings.SEAFLOOR_RATE_LIMIT_WINDOW_SECONDS
        )

    def test_the_schema_records_both_outcomes(self, client: TestClient):
        responses = client.get("/api/v1/openapi.json").json()["paths"]["/api/v1/seafloor"]["get"]["responses"]
        assert responses["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/SeafloorRead")
        assert responses["503"]["content"]["application/json"]["schema"]["$ref"].endswith("/SeafloorUnavailable")

    def test_releases_the_transaction_between_the_limit_and_the_sources(
        self, seafloor_app: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """`get_current_user`'s lookup opened a transaction on the request's session, and a miss
        spends seconds on third parties: the limit is checked, the transaction released, and the
        sources follow with no query between to reopen it."""
        calls: list[str] = []
        session = AsyncMock()
        session.rollback = AsyncMock(side_effect=lambda: calls.append("release"))
        session.execute = AsyncMock(side_effect=lambda *_args, **_kwargs: calls.append("query"))
        seafloor_app.dependency_overrides[async_get_db] = lambda: session

        async def limit(*_args: object) -> None:
            calls.append("limit")

        async def resolve(*_args: object) -> Any:
            calls.append("sources")
            return seafloor.SeafloorRead(status="dry", source=None, grid=None, attempts=[])

        monkeypatch.setattr("src.app.api.v1.seafloor.enforce_rate_limit", limit)
        monkeypatch.setattr("src.app.api.v1.seafloor.resolve_seafloor", resolve)

        assert _seafloor(client, *OPEN_PACIFIC).status_code == 200
        assert calls == ["limit", "release", "sources"]
