from enum import IntEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

# Snake case throughout, like every other enum on the wire. The client words them.
SeafloorSourceId = Literal["noaa_dem", "emodnet", "khoa", "gmrt", "etopo2022"]
SeafloorStatus = Literal["ok", "dry", "failed"]
SeafloorOutcome = Literal[
    "used",
    "no_coverage",
    "not_configured",
    "not_needed",
    "too_coarse",
    "too_little_data",
    "mostly_land",
    "failed",
]
SeafloorLandFrom = Literal["openstreetmap", "soundings"]
SeafloorLandHeightFrom = Literal["aws_terrain", "gmrt", "flat"]


class SeafloorSpan(IntEnum):
    """The sides a view may have, in metres. An enum rather than a `Literal`, because a query
    string's "1000" is coerced into an enum member and refused by an int `Literal`."""

    WIDE = 2000
    NARROW = 1000


class SeafloorGrid(BaseModel):
    """A regular depth grid around the point asked about."""

    # The centre of the south-west cell; every other cell is a whole number of steps from it.
    origin_lat: Annotated[float, Field(description="Latitude of the south-west cell's centre, in degrees.")]
    origin_lon: Annotated[float, Field(description="Longitude of the south-west cell's centre, in degrees.")]
    cell_lat: Annotated[float, Field(description="Distance between rows, in degrees of latitude.")]
    cell_lon: Annotated[float, Field(description="Distance between columns, in degrees of longitude.")]
    rows: Annotated[int, Field(ge=1, description="Number of rows, south to north.")]
    cols: Annotated[int, Field(ge=1, description="Number of columns, west to east.")]
    depths: Annotated[
        list[float | None],
        Field(
            description=(
                "`rows * cols` depths in metres, row-major, the southernmost row first and each row "
                "west to east. Positive is below sea level; negative is land above it. Null is a cell "
                "with no reading. Rounded to the centimetre."
            ),
        ),
    ]
    resolution_meters: Annotated[
        float, Field(description="Roughly how far apart neighbouring cells are, in metres, on the coarser axis.")
    ]
    center_lat: Annotated[float, Field(description="Latitude of the point the grid was fetched around.")]
    center_lon: Annotated[float, Field(description="Longitude of the point the grid was fetched around.")]
    land_from: Annotated[
        SeafloorLandFrom | None,
        Field(
            default=None,
            description=(
                "Where the coastline came from, for a source with no land of its own: OpenStreetMap, or "
                "the gaps between its soundings. Null for a source that measures land itself."
            ),
        ),
    ]
    land_height_from: Annotated[
        SeafloorLandHeightFrom | None,
        Field(
            default=None,
            description=(
                "Where land heights came from, for such a source: AWS Terrain Tiles, GMRT where those "
                "could not be had, or a flat placeholder where neither could. Null otherwise."
            ),
        ),
    ]


class SeafloorAttempt(BaseModel):
    """What happened when one source was considered for the point."""

    source: SeafloorSourceId
    outcome: Annotated[
        SeafloorOutcome,
        Field(
            description=(
                "`used`: this source's grid is the answer. `no_coverage`: it holds nothing here. "
                "`not_configured`: this instance has not been set up to ask it. `not_needed`: a source "
                "tried before it was used. `too_coarse`: its grid has too few cells on a side at this "
                "span. `too_little_data`: too few of its cells have a reading. `mostly_land`: too few "
                "of its readings are under water. `failed`: it could not be asked or its answer could "
                "not be read."
            ),
        ),
    ]
    cell_size_meters: Annotated[
        float | None,
        Field(default=None, description="How fine the source says its data is here, in metres, where it said."),
    ]
    # Set wherever the source's grid was fetched and read: on `used`, `too_coarse`,
    # `too_little_data` and `mostly_land`. Measured before any averaging down to the
    # response's grid, so `used` can differ from the grid's own `cols` and `rows`.
    cols: Annotated[int | None, Field(default=None, description="Columns in the source's grid at this span.")]
    rows: Annotated[int | None, Field(default=None, description="Rows in the source's grid at this span.")]
    resolution_meters: Annotated[
        float | None, Field(default=None, description="The source's grid spacing at this span, in metres.")
    ]
    known_fraction: Annotated[
        float | None,
        Field(default=None, ge=0, le=1, description="Share of the source's cells that have a reading, 0 to 1."),
    ]
    wet_fraction: Annotated[
        float | None,
        Field(default=None, ge=0, le=1, description="Share of those readings that are under water, 0 to 1."),
    ]
    error: Annotated[
        str | None,
        Field(default=None, description="On `failed`, what went wrong, in the source's words where it gave any."),
    ]


class SeafloorRead(BaseModel):
    """The best depth grid for a point, and every source considered for it."""

    status: Annotated[
        SeafloorStatus,
        Field(
            description=(
                "`ok`: `grid` is the answer and `source` gave it. `dry`: the point is on land. "
                "`failed`: no source could be reached, or none gave a usable grid."
            ),
        ),
    ]
    source: Annotated[SeafloorSourceId | None, Field(default=None, description="The source used, on `ok`.")]
    grid: Annotated[SeafloorGrid | None, Field(default=None, description="The depth grid, on `ok`.")]
    attempts: Annotated[
        list[SeafloorAttempt],
        Field(description="One entry per source, in the order they were considered."),
    ]


class SeafloorUnavailable(SeafloorRead):
    """A `failed` answer, which still carries every attempt."""

    detail: Annotated[str, Field(description="A sentence for a caller that reads only errors.")]
