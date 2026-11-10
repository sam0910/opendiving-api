"""Seafloor depth grids around one point: what each source answers, read into one grid shape,
the operations on that grid, and the rule for choosing between sources. Pure, so it is tested
without the network; `services.seafloor` does the fetching.

Depth is in metres, **positive down**: the sources publish elevation, negative under water, and
the sign is flipped once, in the parsers here. A negative depth is land above the waterline,
kept rather than dropped so a coast or an island still has a shape; `None` is a cell the source
had no reading for.

The sources and the rule follow Submersion's site seascape (GPL-3.0, re-implemented rather than
copied): every source that covers the point is asked how fine its data is, they are tried
finest-first, and the first grid that is mostly readings and at least partly water wins.
"""

import math
import re
import struct
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple, Protocol

from ..schemas.seafloor import SeafloorLandFrom, SeafloorLandHeightFrom

# Metres per degree of latitude, and of longitude at the equator.
METRES_PER_DEG_LAT = 110540
METRES_PER_DEG_LON = 111320


@dataclass(frozen=True, slots=True)
class Bounds:
    north: float
    south: float
    east: float
    west: float


@dataclass(frozen=True, slots=True)
class Grid:
    # The centre of the south-west cell.
    origin_lat: float
    origin_lon: float
    cell_lat: float
    cell_lon: float
    rows: int
    cols: int
    # Row-major, row 0 the southernmost.
    depths: list[float | None]
    # Roughly how far apart two neighbouring cells are.
    resolution_meters: float
    # The point the grid was fetched around.
    center_lat: float
    center_lon: float
    # For a source with no land of its own: where its coastline and its land heights came from.
    land_from: SeafloorLandFrom | None = None
    land_height_from: SeafloorLandHeightFrom | None = None


class NoCoverageError(Exception):
    """Raised by a source that answered, but has no data for the area asked about."""


def to_number(value: Any) -> float:
    """A JSON value as JavaScript's `Number()` reads it, which is what the sources' answers were
    first read with: a numeric string is its number, a blank one or null is 0, and anything else
    that is no number is NaN."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    if value is None:
        return 0.0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return math.nan
    return math.nan


def bounds_around(lat: float, lon: float, span_meters: float) -> Bounds:
    """A square `span_meters` on a side, centred on the point."""
    d_lat = span_meters / 2 / METRES_PER_DEG_LAT
    d_lon = span_meters / 2 / (METRES_PER_DEG_LON * max(math.cos(math.radians(lat)), 0.01))
    return Bounds(north=lat + d_lat, south=lat - d_lat, east=lon + d_lon, west=lon - d_lon)


def cell_size_meters(cell_lat: float, cell_lon: float, lat: float) -> float:
    """Roughly how far apart neighbouring cells are, on the coarser of the two axes."""
    return max(cell_lat * METRES_PER_DEG_LAT, cell_lon * METRES_PER_DEG_LON * math.cos(math.radians(lat)))


# --- Choosing between sources ---------------------------------------------------------------

# A source moves ahead of one listed before it only when its data is more than twice as fine:
# close calls keep the listed order, which ranks surveyed regional data over global syntheses.
PREEMPTION_FACTOR = 2


class Candidate(Protocol):
    @property
    def cell_size_meters(self) -> float: ...

    @property
    def holds_place(self) -> bool: ...


def order_by_cell_size[T: Candidate](candidates: Sequence[T]) -> list[T]:
    """Sorts sources, given in preferred order, so a much finer one is tried first. A source that
    `holds_place` is never passed: national survey data outranks a global synthesis whatever cell
    size the synthesis claims."""
    ordered = list(candidates)
    for i in range(1, len(ordered)):
        j = i
        while (
            j > 0
            and not ordered[j - 1].holds_place
            and ordered[j - 1].cell_size_meters > PREEMPTION_FACTOR * ordered[j].cell_size_meters
        ):
            ordered[j - 1], ordered[j] = ordered[j], ordered[j - 1]
            j -= 1
    return ordered


def known_fraction(grid: Grid) -> float:
    """The share of cells with any reading at all."""
    if not grid.depths:
        return 0
    return sum(1 for depth in grid.depths if depth is not None) / len(grid.depths)


def wet_fraction(grid: Grid) -> float:
    """The share of cells with a reading that are under water."""
    known = [depth for depth in grid.depths if depth is not None]
    if not known:
        return 0
    return sum(1 for depth in known if depth > 0) / len(known)


# --- Grid operations ------------------------------------------------------------------------


def downsample(grid: Grid, max_dim: int) -> Grid:
    """Shrinks the grid to at most `max_dim` cells a side by averaging blocks of cells.

    A block's readings are averaged and its empty cells ignored; a block with no reading at all
    stays empty. The origin moves to the centre of the first block.
    """
    row_step = math.ceil(grid.rows / max_dim)
    col_step = math.ceil(grid.cols / max_dim)
    if row_step <= 1 and col_step <= 1:
        return grid

    rows = math.ceil(grid.rows / row_step)
    cols = math.ceil(grid.cols / col_step)
    depths: list[float | None] = []
    for r in range(rows):
        for c in range(cols):
            total = 0.0
            count = 0
            for rr in range(r * row_step, min((r + 1) * row_step, grid.rows)):
                for cc in range(c * col_step, min((c + 1) * col_step, grid.cols)):
                    depth = grid.depths[rr * grid.cols + cc]
                    if depth is not None:
                        total += depth
                        count += 1
            depths.append(None if count == 0 else total / count)

    return replace(
        grid,
        origin_lat=grid.origin_lat + grid.cell_lat * (row_step - 1) / 2,
        origin_lon=grid.origin_lon + grid.cell_lon * (col_step - 1) / 2,
        cell_lat=grid.cell_lat * row_step,
        cell_lon=grid.cell_lon * col_step,
        rows=rows,
        cols=cols,
        depths=depths,
        resolution_meters=grid.resolution_meters * max(row_step, col_step),
    )


def crop_to_bounds(grid: Grid, bounds: Bounds) -> Grid:
    """Trims a grid that came back wider than asked for (a coarse source fetched over a wider
    box) to the cells whose centres fall inside `bounds`. A grid within 10% of the bounds is
    returned as it is."""
    lat_span = (grid.rows - 1) * grid.cell_lat
    lon_span = (grid.cols - 1) * grid.cell_lon
    if lat_span <= (bounds.north - bounds.south) * 1.1 and lon_span <= (bounds.east - bounds.west) * 1.1:
        return grid

    def inside(start: float, cell: float, count: int, lo: float, hi: float) -> tuple[int, int]:
        first = max(0, math.ceil((lo - start) / cell))
        last = min(count - 1, math.floor((hi - start) / cell))
        return first, max(0, last - first + 1)

    first_row, rows = inside(grid.origin_lat, grid.cell_lat, grid.rows, bounds.south, bounds.north)
    first_col, cols = inside(grid.origin_lon, grid.cell_lon, grid.cols, bounds.west, bounds.east)
    depths = [
        grid.depths[r * grid.cols + c]
        for r in range(first_row, first_row + rows)
        for c in range(first_col, first_col + cols)
    ]
    return replace(
        grid,
        origin_lat=grid.origin_lat + first_row * grid.cell_lat,
        origin_lon=grid.origin_lon + first_col * grid.cell_lon,
        rows=rows,
        cols=cols,
        depths=depths,
    )


def depth_at(grid: Grid, lat: float, lon: float) -> float | None:
    """The depth at a point by bilinear interpolation between cell centres, or `None` off the
    grid or where a corner it needs has no reading."""
    row_f = (lat - grid.origin_lat) / grid.cell_lat
    col_f = (lon - grid.origin_lon) / grid.cell_lon
    if row_f < 0 or col_f < 0 or row_f > grid.rows - 1 or col_f > grid.cols - 1:
        return None
    r0 = math.floor(row_f)
    c0 = math.floor(col_f)
    r1 = min(r0 + 1, grid.rows - 1)
    c1 = min(c0 + 1, grid.cols - 1)
    sw = grid.depths[r0 * grid.cols + c0]
    se = grid.depths[r0 * grid.cols + c1]
    nw = grid.depths[r1 * grid.cols + c0]
    ne = grid.depths[r1 * grid.cols + c1]
    if sw is None or se is None or nw is None or ne is None:
        return None
    fr = row_f - r0
    fc = col_f - c0
    return (sw * (1 - fc) + se * fc) * (1 - fr) + (nw * (1 - fc) + ne * fc) * fr


# --- Parsers --------------------------------------------------------------------------------


_ESRI_HEADER_LINE = re.compile(r"^\s*([a-z_]+)\s+(\S+)\s*$", re.IGNORECASE)


def parse_esri_ascii(text: str, center_lat: float, center_lon: float) -> Grid:
    """Reads an ESRI ASCII grid (what GMRT's `format=esriascii` answers) into a depth grid.

    The file lists rows north first; the grid stores them south first. The corner in the header
    is the cell's outer corner, and the origin is moved half a cell in to the cell's centre.
    """
    lines = re.split(r"\r?\n", text)
    header: dict[str, float] = {}
    line = 0
    while line < len(lines) and (match := _ESRI_HEADER_LINE.match(lines[line])):
        header[match[1].lower()] = to_number(match[2])
        line += 1

    def read(key: str) -> float:
        value = header.get(key)
        if value is None or not math.isfinite(value):
            raise ValueError(f'ESRI ASCII grid is missing "{key}"')
        return value

    cols = int(read("ncols"))
    rows = int(read("nrows"))
    xll = read("xllcorner")
    yll = read("yllcorner")
    cell_size = read("cellsize")
    nodata = header.get("nodata_value")

    values = [to_number(token) for token in " ".join(lines[line:]).split()]
    if len(values) < rows * cols:
        raise ValueError(f"ESRI ASCII grid has {len(values)} values, expected {rows * cols}")

    depths: list[float | None] = [None] * (rows * cols)
    for file_row in range(rows):
        row = rows - 1 - file_row
        for col in range(cols):
            elevation = values[file_row * cols + col]
            depths[row * cols + col] = None if elevation == nodata or not math.isfinite(elevation) else -elevation

    return Grid(
        origin_lat=yll + cell_size / 2,
        origin_lon=xll + cell_size / 2,
        cell_lat=cell_size,
        cell_lon=cell_size,
        rows=rows,
        cols=cols,
        depths=depths,
        resolution_meters=cell_size * METRES_PER_DEG_LON * math.cos(math.radians(center_lat)),
        center_lat=center_lat,
        center_lon=center_lon,
    )


def parse_erddap_grid(body: Any, center_lat: float, center_lon: float) -> Grid:
    """Reads an ERDDAP griddap `.json` answer (`{table: {columnNames, rows}}`, one row per cell as
    `[latitude, longitude, value]`) into a depth grid. ERDDAP serves elevation, so the value is
    negated; a null stays empty."""
    table = body.get("table") if isinstance(body, dict) else None
    rows = table.get("rows") if isinstance(table, dict) else None
    if not isinstance(rows, list):
        raise ValueError("ERDDAP answer has no table rows")
    if not all(isinstance(row, list) and len(row) >= 3 for row in rows):
        raise ValueError("ERDDAP answer has a row that is not [latitude, longitude, value]")

    lats = sorted({to_number(row[0]) for row in rows})
    lons = sorted({to_number(row[1]) for row in rows})
    if len(lats) < 2 or len(lons) < 2:
        raise ValueError("ERDDAP answer has fewer than two rows or columns")
    lat_index = {lat: i for i, lat in enumerate(lats)}
    lon_index = {lon: i for i, lon in enumerate(lons)}

    depths: list[float | None] = [None] * (len(lats) * len(lons))
    for row in rows:
        value = row[2]
        r = lat_index.get(to_number(row[0]))
        c = lon_index.get(to_number(row[1]))
        if r is None or c is None:
            continue
        finite = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        depths[r * len(lons) + c] = -float(value) if finite else None

    cell_lat = lats[1] - lats[0]
    cell_lon = lons[1] - lons[0]
    return Grid(
        origin_lat=lats[0],
        origin_lon=lons[0],
        cell_lat=cell_lat,
        cell_lon=cell_lon,
        rows=len(lats),
        cols=len(lons),
        depths=depths,
        resolution_meters=cell_size_meters(cell_lat, cell_lon, center_lat),
        center_lat=center_lat,
        center_lon=center_lon,
    )


# TIFF tags the layout needs, and the two field types they are stored as.
_TIFF_SHORT = 3
_TIFF_LONG = 4


def parse_float32_tiff(data: bytes, bounds: Bounds, center_lat: float, center_lon: float) -> Grid:
    """Reads the one TIFF shape an ArcGIS ImageServer `exportImage` answers with when asked for
    `format=tiff&pixelType=F32`: little-endian, uncompressed, one band of 32-bit floats, in tiles
    or strips. The image covers `bounds` exactly, rows north first. Values are elevations; NaN
    and the huge negative fill some servers use are empty."""
    try:
        return _parse_float32_tiff(data, bounds, center_lat, center_lon)
    except (struct.error, IndexError) as exc:
        raise ValueError("TIFF is truncated") from exc


def _parse_float32_tiff(data: bytes, bounds: Bounds, center_lat: float, center_lon: float) -> Grid:
    if len(data) < 8 or data[:2] != b"II" or struct.unpack_from("<H", data, 2)[0] != 42:
        raise ValueError("Not a little-endian TIFF")

    tags: dict[int, list[int]] = {}
    ifd = struct.unpack_from("<I", data, 4)[0]
    count = struct.unpack_from("<H", data, ifd)[0]
    for i in range(count):
        entry = ifd + 2 + i * 12
        tag, kind, n = struct.unpack_from("<HHI", data, entry)
        # SHORT and LONG are the only types the layout tags use.
        size = 2 if kind == _TIFF_SHORT else 4 if kind == _TIFF_LONG else 0
        if size == 0:
            continue
        at = entry + 8 if n * size <= 4 else struct.unpack_from("<I", data, entry + 8)[0]
        tags[tag] = list(struct.unpack_from(f"<{n}{'H' if size == 2 else 'I'}", data, at))

    def one(tag: int, fallback: int | None = None) -> int:
        values = tags.get(tag)
        value = values[0] if values else fallback
        if value is None:
            raise ValueError(f"TIFF is missing tag {tag}")
        return value

    width = one(256)
    height = one(257)
    if one(258) != 32 or one(339, 1) != 3:
        raise ValueError("TIFF is not 32-bit float")
    if one(259, 1) != 1:
        raise ValueError("TIFF is compressed")
    if one(277, 1) != 1:
        raise ValueError("TIFF has more than one band")

    # Each block (a tile, or a strip as a tile the full width of the image) and where its
    # top-left pixel sits.
    values = [math.nan] * (width * height)
    tile_width = (tags.get(322) or [0])[0]
    tile_height = (tags.get(323) or [0])[0]
    if tile_width and tile_height:
        offsets, block_w, block_h, across = tags.get(324, []), tile_width, tile_height, math.ceil(width / tile_width)
    else:
        offsets, block_w, block_h, across = tags.get(273, []), width, one(278, height), 1
    for b, offset in enumerate(offsets):
        x0 = (b % across) * block_w
        y0 = (b // across) * block_h
        run = min(block_w, width - x0)
        if run <= 0:
            continue
        for y in range(min(block_h, height - y0)):
            start = (y0 + y) * width + x0
            values[start : start + run] = struct.unpack_from(f"<{run}f", data, offset + y * block_w * 4)

    depths: list[float | None] = [None] * (width * height)
    for row in range(height):
        file_row = height - 1 - row
        for col in range(width):
            value = values[file_row * width + col]
            depths[row * width + col] = -value if math.isfinite(value) and value > -1e30 else None

    cell_lat = (bounds.north - bounds.south) / height
    cell_lon = (bounds.east - bounds.west) / width
    return Grid(
        origin_lat=bounds.south + cell_lat / 2,
        origin_lon=bounds.west + cell_lon / 2,
        cell_lat=cell_lat,
        cell_lon=cell_lon,
        rows=height,
        cols=width,
        depths=depths,
        resolution_meters=cell_size_meters(cell_lat, cell_lon, center_lat),
        center_lat=center_lat,
        center_lon=center_lon,
    )


class DepthPoint(NamedTuple):
    lat: float
    lon: float
    # Metres, positive down.
    depth: float


class KhoaPage(NamedTuple):
    total: float
    points: list[DepthPoint]


def parse_khoa_page(body: Any) -> KhoaPage:
    """Reads one page of KHOA's water depth API (`GetWaterDepthApiService`, `type=json`). Result
    code 03 means no data in the box, which is a decline rather than a failure; any other code
    that is not 00 is an error, quota and key problems included."""
    answer: dict[str, Any] = body if isinstance(body, dict) else {}
    header: dict[str, Any] = answer["header"] if isinstance(answer.get("header"), dict) else {}
    content: dict[str, Any] = answer["body"] if isinstance(answer.get("body"), dict) else {}
    code = header.get("resultCode")
    if code == "03":
        raise NoCoverageError("no KHOA data in this area")
    if code != "00":
        message = header.get("resultMsg")
        raise ValueError(
            f"KHOA {code if code is not None else '?'} {message if message is not None else 'unexpected answer'}"
        )

    # A single result arrives as an object rather than a one-item list, and no result as "".
    items = content.get("items")
    raw = items.get("item") if isinstance(items, dict) else None
    entries = raw if isinstance(raw, list) else [raw] if raw else []
    points = []
    for entry in entries:
        item = entry if isinstance(entry, dict) else {}
        point = DepthPoint(
            to_number(item.get("lat", math.nan)),
            to_number(item.get("lot", math.nan)),
            to_number(item.get("dpwt", math.nan)),
        )
        if all(math.isfinite(value) for value in point):
            points.append(point)
    total = content.get("totalCount")
    return KhoaPage(total=to_number(total) if total is not None else len(points), points=points)


# --- Gridding soundings ---------------------------------------------------------------------

# Where land is: True for water, False for land, None where it isn't known.
type WaterTest = Callable[[float, float], bool | None]


@dataclass(frozen=True, slots=True)
class LandRule:
    """How land is decided when gridding soundings that have none on land."""

    # Depth given to a land cell: negative, so it stands above the water. A function gives each
    # land cell its own, from a source that measures land heights.
    depth: float | Callable[[float, float], float]
    # Land where the nearest sounding is farther than this, wherever `is_water` isn't given or
    # doesn't know: see `grid_points`.
    beyond_meters: float
    # A coastline from map data, which wins over the gaps between soundings.
    is_water: WaterTest | None = None


# Each cell asks the coastline at a 3 x 3 spread of spots across its own area, and is land if
# any of them is: an islet narrower than a cell still claims the cell it is in.
_COAST_SAMPLES = (-1 / 3, 0, 1 / 3)


def _weigh(
    local: Sequence[tuple[float, float, float]], indices: Sequence[int], x: float, y: float, radius_squared: float
) -> tuple[float | None, float, float | None]:
    """The inverse-distance-weighted depth at `(x, y)` from the points at `indices` within the
    radius, or None with none there, and the nearest of those points: its squared distance and
    its depth. A point within a metre is taken as it is."""
    weights = 0.0
    total = 0.0
    exact: float | None = None
    nearest_squared = math.inf
    nearest_depth: float | None = None
    for index in indices:
        px, py, depth = local[index]
        dx = px - x
        dy = py - y
        distance_squared = dx * dx + dy * dy
        if distance_squared < nearest_squared:
            nearest_squared = distance_squared
            nearest_depth = depth
        if distance_squared > radius_squared:
            continue
        if distance_squared < 1:
            exact = depth
            continue
        weight = 1 / distance_squared
        weights += weight
        total += weight * depth
    weighted = exact if exact is not None else (total / weights if weights > 0 else None)
    return weighted, nearest_squared, nearest_depth


def grid_points(
    points: Sequence[DepthPoint],
    bounds: Bounds,
    cell_meters: float,
    radius_meters: float,
    center_lat: float,
    center_lon: float,
    land: LandRule | None = None,
) -> Grid:
    """Lays scattered depth points onto a regular grid over `bounds`, `cell_meters` apart, by
    inverse-distance weighting of the points within `radius_meters` of each cell.

    With a `land` rule, a cell is land, and takes `land.depth`, when:

    - `land.is_water` says any part of the cell is land. Map data draws coastlines far finer
      than soundings are spaced, so this shows every islet the map shows. A water cell with no
      sounding inside the radius, in a gap a merged islet left, say, takes the nearest
      sounding's depth.
    - Otherwise (no coastline, or none known there), its nearest point is farther than
      `land.beyond_meters`. For soundings laid on a regular lattice, set that a little past half
      the lattice's diagonal, the farthest any spot can be from a node: then even one missing
      node shows as land.

    Without a rule, only a cell with no point inside the radius is land, left empty.
    """
    metres_per_deg_lon = METRES_PER_DEG_LON * math.cos(math.radians(center_lat))
    cell_lat = cell_meters / METRES_PER_DEG_LAT
    cell_lon = cell_meters / metres_per_deg_lon
    rows = max(2, math.floor((bounds.north - bounds.south) / cell_lat) + 1)
    cols = max(2, math.floor((bounds.east - bounds.west) / cell_lon) + 1)

    # Points in metres from the south-west corner, so distances are plain arithmetic.
    local = [
        ((point.lon - bounds.west) * metres_per_deg_lon, (point.lat - bounds.south) * METRES_PER_DEG_LAT, point.depth)
        for point in points
    ]
    radius_squared = radius_meters * radius_meters

    # Every cell considers only the points in its own bucket and the eight around it, which
    # holds every point within `reach` of it. Visited in their original order, so the weighted
    # sum is the one a pass over every point gives, to the bit. The nearest point is the true
    # nearest when it is within `reach`; past that, a full pass finds it, and only the one case
    # that needs it - a water cell with no point in the radius - pays for that pass.
    reach = max(radius_meters, land.beyond_meters if land else 0.0, 1.0)
    buckets: dict[tuple[int, int], list[int]] = {}
    for index, (x, y, _) in enumerate(local):
        buckets.setdefault((math.floor(x / reach), math.floor(y / reach)), []).append(index)

    neighbourhoods: dict[tuple[int, int], list[int]] = {}

    def nearby(x: float, y: float) -> list[int]:
        key = (math.floor(x / reach), math.floor(y / reach))
        found = neighbourhoods.get(key)
        if found is None:
            found = sorted(
                index for dx in (-1, 0, 1) for dy in (-1, 0, 1) for index in buckets.get((key[0] + dx, key[1] + dy), ())
            )
            neighbourhoods[key] = found
        return found

    everywhere = range(len(local))

    # What the coastline says about a cell: land if any spot in it is land, water if it knows
    # every spot, None if it knows none.
    def coast(lat: float, lon: float) -> bool | None:
        if land is None or land.is_water is None:
            return None
        known = False
        for dy in _COAST_SAMPLES:
            for dx in _COAST_SAMPLES:
                water = land.is_water(lat + dy * cell_lat, lon + dx * cell_lon)
                if water is False:
                    return False
                if water is True:
                    known = True
        return True if known else None

    def land_depth(lat: float, lon: float) -> float:
        assert land is not None
        return land.depth(lat, lon) if callable(land.depth) else land.depth

    used_coast = False
    depths: list[float | None] = [None] * (rows * cols)
    for r in range(rows):
        y = r * cell_meters
        cell_latitude = bounds.south + r * cell_lat
        for c in range(cols):
            x = c * cell_meters
            weighted, nearest_squared, nearest_depth = _weigh(local, nearby(x, y), x, y, radius_squared)
            # Past `reach` the nearest seen may not be the nearest there is.
            nearest_known = nearest_squared <= reach * reach

            cell_longitude = bounds.west + c * cell_lon
            water = coast(cell_latitude, cell_longitude)
            depth_here: float | None
            if water is False:
                depth_here = land_depth(cell_latitude, cell_longitude)
            elif water is True:
                if weighted is not None:
                    depth_here = weighted
                else:
                    depth_here = nearest_depth if nearest_known else _weigh(local, everywhere, x, y, 0)[2]
            elif land is not None and (not nearest_known or nearest_squared > land.beyond_meters**2):
                depth_here = land_depth(cell_latitude, cell_longitude)
            else:
                depth_here = weighted
            if water is not None:
                used_coast = True
            depths[r * cols + c] = depth_here

    return Grid(
        origin_lat=bounds.south,
        origin_lon=bounds.west,
        cell_lat=cell_lat,
        cell_lon=cell_lon,
        rows=rows,
        cols=cols,
        depths=depths,
        resolution_meters=cell_meters,
        center_lat=center_lat,
        center_lon=center_lon,
        land_from=("openstreetmap" if used_coast else "soundings") if land is not None else None,
    )


# --- Map tiles ------------------------------------------------------------------------------


def tile_position(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Where a point falls in the Web Mercator tile grid at zoom `z`, as fractional tiles."""
    n = 2**z
    phi = math.radians(lat)
    return (
        (lon + 180) / 360 * n,
        (1 - math.log(math.tan(phi) + 1 / math.cos(phi)) / math.pi) / 2 * n,
    )


def tiles_covering(bounds: Bounds, z: int) -> list[tuple[int, int]]:
    """The tiles at zoom `z` that a box touches, as `(x, y)`."""
    west, north = tile_position(bounds.north, bounds.west, z)
    east, south = tile_position(bounds.south, bounds.east, z)
    return [
        (x, y)
        for x in range(math.floor(west), math.floor(east) + 1)
        for y in range(math.floor(north), math.floor(south) + 1)
    ]


# A polygon from a vector tile: its rings, in that tile's own pixel coordinates.
type Ring = list[tuple[float, float]]


@dataclass(frozen=True, slots=True)
class TilePolygon:
    rings: list[Ring]


@dataclass(frozen=True, slots=True)
class WaterTile:
    """The water polygons of one vector tile."""

    z: int
    x: int
    y: int
    # Pixels a side, 4096 for most tiles.
    extent: int
    polygons: list[TilePolygon]


# Horizontal bands a tile's edges are filed under, so a crossing test reads only the edges that
# can cross its row.
_EDGE_BANDS = 128


@dataclass(slots=True)
class _Crossings:
    """One tile's polygon edges, and for each row asked about, where each polygon's edges cross
    it - which is all the even-odd rule needs to answer any point on that row."""

    tile: WaterTile
    bands: list[list[tuple[int, float, float, float, float]]] = field(default_factory=list)
    band_height: float = 1.0
    rows: dict[float, list[list[float]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.band_height = max(self.tile.extent, 1) / _EDGE_BANDS
        self.bands = [[] for _ in range(_EDGE_BANDS)]
        for index, polygon in enumerate(self.tile.polygons):
            for ring in polygon.rings:
                for i in range(len(ring)):
                    ax, ay = ring[i]
                    bx, by = ring[i - 1]
                    if ay == by:
                        continue  # Never crosses a row: `(ay > py) != (by > py)` is false.
                    for band in range(self._band(min(ay, by)), self._band(max(ay, by)) + 1):
                        self.bands[band].append((index, ax, ay, bx, by))

    def _band(self, y: float) -> int:
        return min(max(math.floor(y / self.band_height), 0), _EDGE_BANDS - 1)

    def inside(self, px: float, py: float) -> bool:
        crossings = self.rows.get(py)
        if crossings is None:
            crossings = [[] for _ in self.tile.polygons]
            for index, ax, ay, bx, by in self.bands[self._band(py)]:
                if (ay > py) != (by > py):
                    crossings[index].append((bx - ax) * (py - ay) / (by - ay) + ax)
            for xs in crossings:
                xs.sort()
            self.rows[py] = crossings
        # A point is inside a polygon when an odd number of its edges cross the row to its
        # right: the ray cast of the even-odd rule, rings and holes alike.
        return any((len(xs) - bisect_right(xs, px)) % 2 == 1 for xs in crossings)


def water_test(tiles: Sequence[WaterTile]) -> WaterTest:
    """A water test from vector-tile water polygons: water inside any polygon of the tile the
    point falls in, land outside them all, and None off every tile given."""
    by_key = {(tile.x, tile.y): _Crossings(tile) for tile in tiles}
    z = tiles[0].z if tiles else None

    def test(lat: float, lon: float) -> bool | None:
        if z is None:
            return None
        x, y = tile_position(lat, lon, z)
        crossings = by_key.get((math.floor(x), math.floor(y)))
        if crossings is None:
            return None
        tile = crossings.tile
        return crossings.inside((x - tile.x) * tile.extent, (y - tile.y) * tile.extent)

    return test


def terrarium_elevation(red: int, green: int, blue: int) -> float:
    """A Terrarium-encoded elevation pixel (AWS Terrain Tiles) in metres above sea level."""
    return red * 256 + green + blue / 256 - 32768


@dataclass(frozen=True, slots=True)
class TerrainTile:
    """One decoded terrain tile: elevations in metres, row by row from its north-west."""

    x: int
    y: int
    # Pixels a side.
    size: int
    elevations: Sequence[float]


def terrain_sampler(tiles: Sequence[TerrainTile], z: int) -> Callable[[float, float], float | None]:
    """Elevation at a point by bilinear interpolation between pixel centres, across tile edges,
    or None where a needed pixel is on no tile given."""
    by_key = {(tile.x, tile.y): tile for tile in tiles}
    size = tiles[0].size if tiles else 256

    def pixel(gx: int, gy: int) -> float | None:
        tile = by_key.get((gx // size, gy // size))
        if tile is None:
            return None
        return tile.elevations[(gy - tile.y * size) * size + gx - tile.x * size]

    def sample(lat: float, lon: float) -> float | None:
        x, y = tile_position(lat, lon, z)
        # Global pixel coordinates, shifted so whole numbers fall on pixel centres.
        fx = x * size - 0.5
        fy = y * size - 0.5
        x0 = math.floor(fx)
        y0 = math.floor(fy)
        nw = pixel(x0, y0)
        ne = pixel(x0 + 1, y0)
        sw = pixel(x0, y0 + 1)
        se = pixel(x0 + 1, y0 + 1)
        if nw is None or ne is None or sw is None or se is None:
            return None
        tx = fx - x0
        ty = fy - y0
        return (nw * (1 - tx) + ne * tx) * (1 - ty) + (sw * (1 - tx) + se * tx) * ty

    return sample
