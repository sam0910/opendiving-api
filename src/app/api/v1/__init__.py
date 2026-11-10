from fastapi import APIRouter

from .admin import router as admin_router
from .api_keys import router as api_keys_router
from .auth import router as auth_router
from .certifications import router as certifications_router
from .checkin_details import router as checkin_details_router
from .checkin_links import router as checkin_links_router
from .commons_export import router as commons_router
from .config import router as config_router
from .contacts import router as contacts_router
from .courses import router as courses_router
from .dive_conditions import router as dive_conditions_router
from .dive_drafts import router as dive_drafts_router
from .dive_form_presets import router as dive_form_presets_router
from .dive_site_proposals import admin_router as dive_site_proposals_admin_router
from .dive_site_proposals import router as dive_site_proposals_router
from .dive_sites import router as dive_sites_router
from .dives import router as dives_router
from .download_tickets import router as download_tickets_router
from .export import router as export_router
from .gear_items import router as gear_items_router
from .gear_service import router as gear_service_router
from .gear_sets import router as gear_sets_router
from .geocoding import router as geocoding_router
from .health import router as health_router
from .invitations import router as invitations_router
from .join_channels import router as join_channels_router
from .logbook_import import router as logbook_import_router
from .map_tiles import router as map_tiles_router
from .passkeys import router as passkeys_router
from .people import router as people_router
from .seafloor import router as seafloor_router
from .sessions import router as sessions_router
from .species import router as species_router
from .support import router as support_router
from .tags import router as tags_router
from .trips import router as trips_router
from .users import router as users_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(commons_router)
router.include_router(config_router)
router.include_router(join_channels_router)
router.include_router(auth_router)
router.include_router(users_router)
router.include_router(passkeys_router)
router.include_router(sessions_router)
router.include_router(api_keys_router)
router.include_router(invitations_router)
router.include_router(admin_router)
router.include_router(trips_router)
router.include_router(courses_router)
router.include_router(contacts_router)
router.include_router(people_router)
router.include_router(tags_router)
router.include_router(dive_sites_router)
router.include_router(dive_site_proposals_router)
router.include_router(dive_site_proposals_admin_router)
router.include_router(geocoding_router)
router.include_router(seafloor_router)
router.include_router(gear_items_router)
router.include_router(gear_sets_router)
router.include_router(gear_service_router)
router.include_router(certifications_router)
router.include_router(checkin_details_router)
router.include_router(checkin_links_router)
router.include_router(download_tickets_router)
router.include_router(species_router)
router.include_router(dives_router)
router.include_router(dive_drafts_router)
router.include_router(dive_conditions_router)
router.include_router(map_tiles_router)
router.include_router(dive_form_presets_router)
router.include_router(support_router)
router.include_router(export_router)
router.include_router(logbook_import_router)
