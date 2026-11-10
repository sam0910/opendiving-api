"""The seafloor explorer's depth grid, fetched from public bathymetry services on the caller's
behalf (`services.seafloor`).

Like geocoding, nothing here is stored or owned: no uuid, no ownership check and no `@cache`.
The service caches each answer under a global key, and a per-user limit bounds what one account
can make this instance ask of third parties.

`failed` is a 503, the status this API gives a provider it could not ask, but the body is the
whole answer plus `detail`: the attempts are what tell a diver why there is no seafloor to see.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db, release_read_transaction
from ...core.utils.rate_limit import enforce_rate_limit
from ...schemas.seafloor import SeafloorRead, SeafloorSpan, SeafloorUnavailable
from ...services.seafloor import caller_budget_key, resolve_seafloor

router = APIRouter(tags=["seafloor"])

_UNAVAILABLE = "No seafloor data could be fetched for this point. Try again later."


@router.get(
    "/seafloor",
    response_model=SeafloorRead,
    responses={503: {"model": SeafloorUnavailable, "description": "No source gave a usable grid."}},
)
async def read_seafloor(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    lat: Annotated[float, Query(ge=-85, le=85, description="Latitude of the point, in decimal degrees.")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude of the point, in decimal degrees.")],
    span: Annotated[
        SeafloorSpan, Query(description="Side of the square around the point, in metres: 2000 or 1000.")
    ] = SeafloorSpan.WIDE,
) -> SeafloorRead | JSONResponse:
    """The best seafloor depth grid for a square `span` metres across, centred on a point, and a
    record of every data source considered for it.

    The point is rounded to four decimal places (about 10 m) first, so `center_lat` and
    `center_lon` may differ from what was sent. Depths are metres, positive below sea level and
    negative on land above it, null where a cell has no reading; the grid has at most 160 cells
    a side, its southernmost row first.

    `200` with `status` `ok` and a `grid`, or `dry` when the point is on land. `503` when no
    source could be reached or none gave a usable grid; that body carries `status` `failed`,
    the attempts and a `detail`. An answer is the same for every caller and is kept for up to a
    week; a failure is not kept.

    Rate limited per account; a request over it is a 429.
    """
    await enforce_rate_limit(
        caller_budget_key(current_user["id"]),
        settings.SEAFLOOR_RATE_LIMIT_PER_USER,
        settings.SEAFLOOR_RATE_LIMIT_WINDOW_SECONDS,
    )
    # `get_current_user`'s lookup opened a transaction on this session; a miss below spends
    # seconds on third parties, and must not hold a pooled connection idle the whole time.
    await release_read_transaction(db)

    result = await resolve_seafloor(lat, lon, int(span))
    if result.status == "failed":
        body = SeafloorUnavailable(**result.model_dump(), detail=_UNAVAILABLE)
        return JSONResponse(status_code=503, content=body.model_dump(mode="json"))
    return result
