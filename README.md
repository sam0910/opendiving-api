# OpenDiving API

**The backend component of [OpenDiving](https://github.com/opendiving/opendiving) — an open-source,
self-hostable dive log.** FastAPI + PostgreSQL + Redis.

Your dive history should outlive any app. This API keeps every original dive-computer export
alongside the dive it recorded, serves the lot over a clean, documented REST API, and takes the
whole log back out in open formats in a single request — and back **in** again, so your data is
never more than one `curl` away in either direction. Run your own copy and the Postgres database
underneath it is yours too.

## Looking to run OpenDiving?

**Start at [opendiving/opendiving](https://github.com/opendiving/opendiving).** That is the front
door: the pitch, the four-command install, the release the compose file comes from, and every
self-hosting guide — installing, configuring, reverse proxies, backup and restore, upgrades and
troubleshooting.

This repository is one of the two application containers that install brings up, alongside
[opendiving-web](https://github.com/opendiving/opendiving-web). It publishes an image and nothing
else; what is here is the source, and the notes for working on it.

## What it does

- **Dive log API** — dives with gas mixtures (O₂/He, pressures), multiple ordered dive sites per
  dive, trips, weights, water type, altitude, the kind of dive, a rating, tags, the air temperature,
  current, waves and weather, how the diver got in and the boat's name, and notes. Soft deletes
  throughout.
- **Dive sites** — each with its other names, its entries in OpenStreetMap or Wikidata, the depth
  range dived there, the water, the altitude, how divers get in and tags from the same vocabulary as
  dives, and a summary of your own dives there: how many, the last, the deepest, the species seen
  and your mean rating. The list sorts on that summary and filters by tag.
- **Technical diving** — per-cylinder ppO₂ limits and gas roles, each computer's CNS/OTU and surface
  pressure persisted from imports, and per-tank gas consumption derived from recorded gas switches
  on multi-tank dives.
- **Dive-computer files on the dive form** — upload one file of one dive, in any format logbook
  import reads, to `POST /dive/parse` and get the dive back to pre-fill a form; the same reader, the
  `divejson` package, reads both doors, and a file either door keeps gives the same recording. A
  file of several dives, or of one dive two computers recorded, goes through logbook import instead.
  Attach the file to the dive afterwards and the **per-sample profile** (depth, temperature, tank
  pressure, deco ceiling, the diver's heart rate where the computer measured it, **the computer's
  own no-decompression clock, time to surface, ppO₂, CNS clock and both gradient factors**, dive
  events) is extracted server-side and served with ETag caching, its summary stating the maximum and
  average heart rate.
- **Recordings** — a dive holds what recorded it, in order, and each of those holds its own files
  and its own profile. So a diver on two computers keeps both accounts of the dive, the same
  computer exported twice fills one record rather than making two, and every device the file named —
  brand, model, serial, firmware, its own name and its own dive counter — is kept beside the
  samples, along with **the mode, salinity and decompression model that computer ran with, and its
  CNS, OTU and surface-pressure readouts**, all of which two computers on one dive legitimately
  disagree about. A computer that shut down mid-water logs the dive twice, so two dives can be
  **merged** into one: the two records land on a single axis with the stretch the computer was off
  left empty, and nothing is invented to bridge it.
- **Air consumption** — SAC and RMV derived automatically, per tank on multi-tank dives, plus a
  gas-use history endpoint powering the dashboard trend chart.
- **Species log** — record what you saw on a dive, and for each species how many you counted or a
  band — few, many, abundant — and a note if you like, from a catalog searched live against
  [WoRMS](https://www.marinespecies.org/) for the taxonomy and [Wikidata](https://www.wikidata.org/)
  for the common names — so typing "clownfish" finds *Amphiprion ocellaris*, which WoRMS alone would
  not. Catalog rows are shared across the instance, and search falls back to what is already stored
  rather than failing when a register is unreachable. Distinct species seen is part of the dive
  statistics, and `GET /user/species` is the whole life list: every species you have logged, with
  how many dives saw it and when. The taxonomy is WoRMS's, whose text content is available under
  [CC BY](https://creativecommons.org/licenses/by/4.0/) and which asks to be cited in full: *WoRMS
  Editorial Board (2026). World Register of Marine Species. Available from
  https://www.marinespecies.org at VLIZ. Accessed 2026-09-11. doi:10.14284/170* — an accessed date a
  README cannot keep current, and does not need to, because it stands for no copy of anything:
  **this app queries the register live rather than holding a snapshot of it**, so an instance holds
  whatever WoRMS answered on the days its divers went looking.
- **Species photos** — a freely licensed photograph per species, chosen from
  [Wikimedia Commons](https://commons.wikimedia.org/), fetched and stored by this instance itself,
  so no visitor's browser ever contacts Wikimedia. A species whose candidates cannot be told apart
  gets no photo rather than a picture of a different animal, nor does one whose only file is
  narrower than 500 px; the author, licence and source travel with it so a credit line can be
  rendered. The operator can hide a photo, pin another file from Commons, or ask the rule again.
- **OpenDiving Commons** — an open pool of real dive sites, bundled in the image, so a picker can
  offer "SS Thistlegorm" rather than only the town it is near. A geocoder knows where Dahab is, not
  where the Blue Hole's north entry is. A Commons site is one shared record: your dive names it,
  your notes and tags sit on top of it, and nobody else's reach you. No account, no key and no
  outbound call: the records ship with the app, and every one carries the credit its source asks
  for.
- **Surface conditions** — off until the operator names a provider: the worker then asks
  [Open-Meteo](https://open-meteo.com/) for the weather, sea state and tide at each dive's place and
  hour, fills the air temperature, weather and waves the diver left empty, and marks each value it
  wrote until the diver replaces it. Data CC BY 4.0, credited wherever it is shown.
- **Gear** — items, a cylinder's capacity, working pressure and material on the item and a dive's
  tank linked to it, gear sets with default weights, service **schedules** (by months, dives or
  hours of diving), service history, a due-soon endpoint, and a scheduled email reminder digest.
- **Certifications** — c-card records with front/back card images, and an email when a card or one
  of the diver's insurance policies is about to expire and again when it has.
- **Year in review** — each January, an email with the diver's previous year in figures.
- **Training courses** — the course a card came out of and the dives logged on it, with the agency,
  status, the people on it and the contact that ran it. No mainstream logbook models this: the
  agency apps tie dives to a course *or* cards to a course, and none of them let the record outlive
  the agency.
- **Contacts** — the dive centers, schools, shops, clubs and places you stayed, each kept once with
  its phone, email, website, address and position and picked for a dive, a course, a card, a gear
  service or a trip part. Imported from UDDF's dive bases, shops and accommodation.
- **People** — the buddies, guides, instructors, fellow students and companions you were with, each
  kept once and listed with a role on a dive, a trip or a course; a course's and a card's instructor
  is one. A person can be linked to an account on the same instance by its exact username, which
  shows you that account's current username and tells it nothing. Imported from UDDF buddies and
  guides and Subsurface's buddy and divemaster fields.
- **Full export** — everything out in open formats (DiveJSON, UDDF, CSV, and an archive of all three
  plus your dive drafts and your original files) in one request. Owner-only, never cached; the
  DiveJSON validates against the published 1.0 schema and the UDDF against the 3.2.2 one.
- **[DiveJSON](https://divejson.org)** — the open dive-log interchange format this project
  maintains, and this is its reference implementation: a lossless structured copy of the whole
  logbook, where UDDF measurably loses trips, gear, weights and UTC offsets.
- **Logbook import** — put any number of files into an account at once, in any mix of the formats it
  reads. They are uploaded once and held on the server, with a review of each dive that is new, what
  changes on the ones you have, and what is skipped, which any device of the account can read; the
  apply writes the lot as reviewed in one transaction, and the import stays in its history. A
  DiveJSON document or a full-export archive goes in as it is; a UDDF file, a Subsurface `.ssrf`, a
  FIT file, a Suunto app export, a Suunto DM5 XML export, a Shearwater Cloud database or XML export,
  or a `.zip` of any of those is converted on the way in by the
  [`divejson`](https://pypi.org/project/divejson/) package, and each dive's row lists what its files
  did not record, what the import worked out, and which of the dive's values it left out. A file
  that is one dive is kept on the dive it becomes, and one recording exported twice - a Suunto's FIT
  beside its JSON - is one recording with both files. Restore a backup, migrate between instances,
  or bring a logbook across from whatever you were keeping it in.
- **Passwordless auth** — email sign-in (over SMTP, so any relay or provider works), Google Sign-In,
  and passkeys, with automatic account linking, short-lived access tokens, and httpOnly refresh
  cookies. The sign-in email carries a magic link *and* a six-digit code, so reading your mail on a
  different device than you started on still works. A passkey is a third *first* factor rather than
  a second one — unphishable and one tap, and the only method needing no third-party service at all.
  No password storage at all. The full design, with sequence diagrams, is in
  [docs/authentication.md](docs/authentication.md).

## Planned

- **More formats in** — a reader is a contribution to the `divejson` package rather than a change
  here, and this app reads whatever the release it pins registers.
- **Public share links** — read-only dive/trip pages.
- **Statistics endpoints** — records, per-year aggregates, site maps, a life list.

## Running it from source

For working on it, rather than for running it — this compose file builds from `./src`, mounts it for
live reload, and publishes the API and Postgres on the host. To *run* an instance rather than
develop against one, install it from
[opendiving/opendiving](https://github.com/opendiving/opendiving) instead.

```bash
git clone https://github.com/opendiving/opendiving-api.git
cd opendiving-api
cp src/.env.example src/.env
openssl rand -hex 32           # put this in SECRET_KEY, and change POSTGRES_PASSWORD
docker compose up
```

That's the backend: the API on [http://localhost:8000](http://localhost:8000) (interactive docs at
`/docs`), PostgreSQL, Redis, and an [arq](https://arq-docs.helpmanual.io/) worker for emails and
scheduled jobs. Pair it with [opendiving-web](https://github.com/opendiving/opendiving-web) for the
frontend.

Inside the opendiving workspace (this checkout with `../run.sh` beside it) the same stack runs
without Docker, started and stopped by `../run.sh`; the workspace's `../RUN.md` says how.

### Configuration

An *installed* instance is configured through the `.env` beside its compose file —
[the configuration reference](https://github.com/opendiving/opendiving/blob/main/docs/configuration.md)
covers that one. From source, everything is configured through `src/.env`, which starts as a copy of
[`src/.env.example`](src/.env.example) — every setting is listed and commented there. `SECRET_KEY`
is not optional: it signs every token the API issues, the template's value is published in this
repository, and the app **refuses to start** on it rather than letting a deployment run on a key
anyone can read. Change `POSTGRES_PASSWORD` too. The parts worth knowing about:

```bash
# Magic-link emails go out over SMTP. Leave SMTP_HOST unset for local development -
# the sign-in link is then only logged, not emailed. Point this at any relay you
# trust (your provider, your host's, your own); don't run your own MTA unless you
# already know why. Resend users: smtp.resend.com, username "resend", password = the
# API key. EMAIL_FROM_ADDRESS is required as soon as SMTP_HOST is set - startup
# fails without it rather than letting an undeliverable address surface hours later.
# SMTP_HOST itself is required on any ENVIRONMENT but local: sign-in is passwordless,
# so a deployed instance with no relay cannot let anybody in at all, and the logged-link
# fallback would put a working sign-in link in whatever collects that instance's logs.
SMTP_HOST="smtp.example.com"
SMTP_PORT=587
SMTP_TLS_MODE="starttls"       # starttls (587) | tls (465) | none (a local relay only)
SMTP_USERNAME="..."            # both optional: an anonymous relay needs neither
SMTP_PASSWORD="..."
EMAIL_FROM_ADDRESS="noreply@yourdomain.example"

# Used to build magic-link URLs ({FRONTEND_URL}/auth/verify?token=...)
FRONTEND_URL="http://localhost:3000"

# Where the frontend's support form (POST /api/v1/support) delivers to. No default:
# unset, that endpoint answers 503 and the form is simply off, which beats mailing
# your users' support requests to somebody else's inbox.
CONTACT_FORM_EMAIL="you@example.com"

# Optional: Google Sign-In. Both halves of one OAuth client, or neither - an id with no
# secret refuses to start. The id must match the frontend's NEXT_PUBLIC_GOOGLE_CLIENT_ID;
# the secret stays here. Register {FRONTEND_URL}/auth/google/callback as an Authorized
# redirect URI on that client.
GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET="your-client-secret"
```

Token lifetimes, rate limits, and the rest have sensible defaults — they're in `src/.env.example`
commented out, and `src/app/core/config.py` is the authoritative list.

#### Reading outgoing mail locally

With `SMTP_HOST` unset, nothing is sent and the sign-in link is written to the API's log —
`docker compose logs api`, or `../run.sh signin` inside the workspace. That's the intended local
flow and it needs nothing running. To see the mail itself rendered instead, bring up the opt-in
[Mailpit](https://mailpit.axllent.org/) profile and point the app at it:

```bash
docker compose --profile mail up      # inbox at http://localhost:8025
```

```bash
SMTP_HOST="mailpit"
SMTP_PORT=1025
SMTP_TLS_MODE="none"
```

`mailpit` is a compose hostname; without Docker, run Mailpit yourself and use `localhost`.

Comment `SMTP_HOST` back out when you're done: left set with no Mailpit running, sends fail against
a dead host and no link is logged either. Either way, an edit to `src/.env` needs
`docker compose up -d --force-recreate api` to take effect — a plain `restart` reuses the
environment the container was created with. Inside the workspace, `../run.sh restart`.

## API overview

Everything is mounted under `/api/v1`, and the OpenAPI document is the reference for it — generated
from the routes themselves, so it describes the API as it actually is rather than as a list here
last remembered it. Every instance serves it at `/api/v1/openapi.json`, `production` included, with
no token; the interactive page that renders it, `/docs`, is open on `local`, behind a superuser on
`staging` and absent on `production`.

The families: **auth** (email link or six-digit code, Google, passkeys, refresh, sign-out, account
restore) and **user** (profile, avatar, check-in portrait, the check-in details, the check-in link,
email change, dive statistics, gas-use history, the species life list, species suggestions for the
dive form, account deletion); **API keys**, which a diver creates, lists and revokes for tools of
their own; **dives**, the bulk of it, with their **recordings** — attach an export, read one
recording's per-sample profile back, fold two dives into one — alongside **trips**, **dive sites**
with the public **Commons** export where this instance is its primary, the shared **species**
catalog a dive can reference, a **geocoding** helper for naming a site pinned on a map, and the
**map tiles** every card and page head is composed from, drawn once for the whole instance, with the
**map style** they are drawn on; **gear** as items, sets, service schedules and service records;
**certifications** with their card images and the **courses** that issued them; **export** in
DiveJSON, UDDF, CSV or full-archive form and **import** back from either of the first and the last,
or from any format the converter reads; **invitations**, which exist only where the operator has
closed registration (`REGISTRATION_MODE`, documented with the rest of the settings in
`src/.env.example`) — a member sends and revokes their own, makes, resets and removes their invite
link, and the routes answer 404 on an open instance; and **admin**, the operator's own — the queue
of people who have asked to be let in, inviting or removing them in a batch, the daily totals of
accounts created, sign-ins and active accounts, and the species catalog with its photos to hide, pin
or re-fetch — which is the one family gated on `is_superuser` rather than merely on having a token.
All of those want a bearer token: a browser's access token, or for a tool of your own an **API key**
— created in Settings, `read` or `write`, lasting thirty days, ninety, a year or until revoked, and
sent on the same `Authorization: Bearer` header. A key is refused on the routes that manage
credentials, the sign-in address or the account itself, and a `read` key on anything but a safe
method; `docs/authentication.md` has the rest. The ones that don't are **support**, the auth routes
themselves, the two health checks — `/health` says the process is up, `/health/ready` says Postgres
and Redis answered, and 503s when they didn't — `POST /invite-requests`, which is how somebody with
no account asks a closed instance for an invitation, `GET /config`, which tells the web app whether
registration is open - whether the project itself operates the instance, whether any join link
exists (`join_links`), and whether a map renderer draws the map tiles (`map_tiles`) and the credit
they owe (`map_attribution`) - before anyone has signed in, `GET /join-channel/{slug}`, which says
whether one join link is live, `GET /invite-link/{token}`, which says whose invite link one token
is, `GET /species/{uuid}/photo`, which serves a public Commons image to an `<img>` tag that has no
way to send a token, and `GET /checkin/{token}` with its `/portrait` and
`/certification/{uuid}/front`, the check-in page a diver shared as a link, where the token in the
path is the credential, and `GET /openapi.json`, the document itself.
`tests/test_route_authentication.py` is the guard that keeps the *anonymous* half of that list
honest — it compares the app's real route table against its own allowlist and holds the reason for
each — but nothing checks this paragraph, so a new route family belongs here by hand.

A record's create — `POST /dive`, `/trip`, `/person` and the like; `/docs` lists the header on each
— takes an optional `Idempotency-Key` header: a repeat under the same key answers the record the
first one made, as it now reads, instead of making another.

A **dive draft** is a dive not yet logged, saved and finished from either device:
`POST /dive-draft`, `GET /dive-drafts`, `GET`/`PATCH`/`DELETE /dive-draft/{uuid}`, and
`POST /dive-draft/{uuid}/dive`, which logs the dive as `POST /dive` does and deletes the draft in
the same request.

A typical import flow:

```bash
# 1. Parse an export to pre-fill the dive form
curl -X POST http://localhost:8000/api/v1/dive/parse \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@Dive_2026-04-17.json"

# 2. Create the dive, then attach the original file to it. The response says which
#    *recording* it landed in: a second export of a computer the dive already has
#    fills that record, and a different computer becomes a recording of its own.
curl -X POST http://localhost:8000/api/v1/dive/{uuid}/recordings \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@Dive_2026-04-17.json" -F "file_token=$FILE_TOKEN"

# 3. One recording's per-sample profile, served with an ETag. A diver on two computers
#    has two of them; `GET /dive/{uuid}` lists the recordings and their uuids.
curl http://localhost:8000/api/v1/dive/{uuid}/recording/{rid}/profile -H "Authorization: Bearer $TOKEN"
```

And a whole logbook, in and out:

```bash
# Out: one DiveJSON document, or a zip with the stored files beside it
curl -OJ http://localhost:8000/api/v1/export/divejson -H "Authorization: Bearer $TOKEN"

# Back in. The upload holds the files and answers with the review, writing nothing to the
# logbook; `uuid` in the answer names the import. Send `file` once per file, in any format the
# converter reads - `dives.uddf`, `Dive.fit` beside `Dive.json`, `garmin-export.zip`.
curl -X POST http://localhost:8000/api/v1/imports \
  -H "Authorization: Bearer $TOKEN" -F "file=@Dive.fit" -F "file=@Dive.json"

# Read it again from anywhere, with what its files did not record in its log.
curl http://localhost:8000/api/v1/imports/pending -H "Authorization: Bearer $TOKEN"
curl http://localhost:8000/api/v1/import/{uuid}/notes -H "Authorization: Bearer $TOKEN"

# Write it as reviewed. No bytes travel again.
curl -X POST http://localhost:8000/api/v1/import/{uuid}/apply \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'
```

## Development notes

- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, the checks CI runs, and how a new format arrives.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability privately. Please don't open a public
  issue for one.
- `docs/authentication.md` — the auth design, with sequence diagrams for every flow.
- `DECISIONS.md` — non-obvious choices and gotchas (schema-change workflow, check constraints,
  caching strategy…). Read it before your first PR.
- Schema changes ship an Alembic revision and are applied on startup. A dev database created before
  migrations landed needs one `alembic stamp head` — see [CONTRIBUTING.md](CONTRIBUTING.md).
- Tests: `uv run pytest`, or containerised —
  `docker compose -f docker-compose.yml -f docker-compose.test.yml up --build --abort-on-container-exit api`.
  `docker-compose.test.yml` is an overlay and does nothing on its own. Note that the Postgres-backed
  tests skip themselves unless the test process can reach the database; see
  [CONTRIBUTING.md](CONTRIBUTING.md).
- Re-read stored profiles after a release that moves the `divejson` pin, the `divejson-raw` image
  the `Dockerfile` pins, or the extractor version:
  `docker compose exec api python -m src.scripts.backfill_dive_profiles` (inside the workspace,
  `.venv/bin/python -m src.scripts.backfill_dive_profiles`). Until it runs, every profile is served
  as the previous reader left it.
- Once, after that: `docker compose exec api python -m src.scripts.backfill_dive_figures --dry-run`,
  then without `--dry-run`. It rewrites each dive's submerged time and average depth that still hold
  the whole recording's figures to its time in the water, and leaves a value a diver typed. A
  logbook-imported dive moves only for a device named with `--device BRAND [MODEL]`; the script's
  docstring says how to choose them.
- Once, after the release that ends the dive-card outline at the dive's end:
  `docker compose exec api python -m src.scripts.backfill_depth_outlines --dry-run`, then without
  `--dry-run`. It rewrites each stored outline from its profile's samples and nothing else; until it
  runs, the dive list draws the outlines stored before.
- Once, after the release that takes a fold's start and end values by side:
  `docker compose exec api python -m src.scripts.backfill_folded_dives --dry-run`, then without
  `--dry-run`. It ends each dive merged before on the later record's CNS, OTU, tank pressure and
  exit fix, lists every value it changes, and names each dive whose cylinders and fixes it leaves
  for you.

## Related repositories

|                                                                |                                                     |
| -------------------------------------------------------------- | --------------------------------------------------- |
| [opendiving](https://github.com/opendiving/opendiving)         | **The front door** — install, docs, and the release |
| [opendiving-web](https://github.com/opendiving/opendiving-web) | Next.js web app                                     |
| [opendiving-ios](https://github.com/opendiving/opendiving-ios) | SwiftUI app, in development, not in the App Store   |

## License

[AGPL-3.0](LICENSE). Run it, change it, self-host it freely — but if you offer a modified version as
a service, you share your changes. Divers' data stays free.

**Two bundled data files are not covered by it**, because the AGPL is a *software* copyleft and says
nothing about a vendored database. Both record their own licence inside themselves, where it cannot
drift away from the data:

- `src/app/data/commons_dive_sites.geojson` — the records of
  [OpenDiving Commons](https://github.com/opendiving/opendiving-commons), a Derivative Database of
  OpenStreetMap under the
  [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). © OpenDiving
  Commons contributors. © OpenStreetMap contributors, <https://osm.org/copyright>. It also carries
  public-domain (CC0) records from Wikidata and from divers' contributions, and public-domain
  records from NOAA's Florida Keys and Thunder Bay National Marine Sanctuaries and the Florida Fish
  and Wildlife Conservation Commission, and its locality names come from Natural Earth (public
  domain). Its positions are not for navigation. Redistributing it — which anyone who redistributes
  this repository or an image built from it does — carries ODbL's share-alike and notice conditions
  with it.
- `src/app/data/marine_areas.geojson` — Natural Earth sea polygons, public domain.

Both are regenerated by the scripts in `scripts/`, which is what makes the method of producing them
available alongside the result (ODbL §4.6). See
[CONTRIBUTING.md](CONTRIBUTING.md#refreshing-the-vendored-data-files).
