# Contributing to OpenDiving API

Thanks for wanting to help. Bug reports, a fix for a typo in a docstring, a sample export from a
dive computer nobody has covered yet — all welcome.

For anything bigger than a small fix, **open an issue first** so we can agree on the shape before
you spend an evening on it. That is especially true for changes to the database schema or the public
API surface, since [opendiving-web](https://github.com/opendiving/opendiving-web) and
[opendiving-ios](https://github.com/opendiving/opendiving-ios) consume it.

Participation is covered by our [Code of Conduct](CODE_OF_CONDUCT.md).

Found a security vulnerability? Don't open an issue — [SECURITY.md](SECURITY.md) says where to send
it privately.

## Getting set up

The whole stack runs from Docker:

```bash
git clone https://github.com/opendiving/opendiving-api.git
cd opendiving-api
cp src/.env.example src/.env
openssl rand -hex 32           # SECRET_KEY; the rest of the defaults are fine locally
docker compose up
```

That gives you the API on <http://localhost:8000> (docs at `/docs`), PostgreSQL, Redis, and the arq
worker. Leave `SMTP_HOST` unset locally — sign-in URLs, and the six-digit code that rides the same
email, are then written to the API logs instead of emailed, which is what you want for development.

Inside the opendiving workspace — this checkout cloned into it, with `../run.sh` beside it — the
stack runs without Docker instead: Homebrew Postgres and Redis, and the API and worker as plain
processes that `../run.sh` starts, stops and tails. The workspace's `../RUN.md` says how, and
wherever this file says `docker compose`, that is the Docker path.

For running the tooling (ruff, mypy, pytest) outside the container you need Python 3.15 and
[uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
```

## Before you open a PR

Three workflows run on every pull request, and all must be green. Run them locally first:

```bash
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mdformat --check *.md docs .github tests
uv run mypy src --config-file pyproject.toml
uv run mypy tests --config-file pyproject.toml
uv run mypy scripts --config-file pyproject.toml
uv run pytest -n auto --dist loadgroup --cov=src/app --cov-report=term-missing
```

If your PR touches `src/app/models/`, add the migration drift check — CI runs it and the suite does
not (see *Things that will bite you* below):

```bash
cd src && POSTGRES_SERVER=localhost uv run alembic check
```

Note that lint and type-checking cover `tests/` and the build-time `scripts/` as well as `src/`.
mypy runs as three separate invocations, for two different reasons. `src` and `tests` are split
because the app is importable as both `app.*` (via `mypy_path`) and `src.app.*` (how the tests
import it), and asking it to check both roots at once fails with "source file found twice under
different module names". `scripts` is third only because it sits under neither root; it needs no
environment, since mypy reads what `build_commons_bundle.py` imports from the app without running
it.

mypy and pytest need `ENVIRONMENT=local` and a `SECRET_KEY` in the environment. Any value except a
placeholder: `Settings._reject_placeholder_secret_key` rejects the one `src/.env.example` ships and
a handful of stock stand-ins (`changeme`, `secret`, …), in every environment, so a deployment cannot
run on a published key. CI uses a throwaway `test-secret-key-for-testing-only`, which is fine on
`ENVIRONMENT=local` — the guard is a list of known placeholders, not an entropy check — and refused
everywhere else, since it is published here.

**The suite runs without a database.** Almost every test mocks the session (`mock_db`), so a cold
checkout with nothing else running gives you a green run in under a second. The exceptions are the
modules that insert real rows through a session to verify what Postgres itself settles — the
constraints it enforces, the window functions a renumber runs, whether a query filtered by `user_id`
at all. They share one `skipif(not db_available())` guard from `tests/conftest.py`, so with no
database reachable they **skip silently** rather than fail — and that shared guard is also how you
find them: `grep -rl 'skipif(not db_available' tests/`. Deliberately not listed by name here, and
not counted either, because the set grows with nearly every change to the suite and a list in a doc
is the copy that stops being true. (Grep the guard, not the bare `db_available` — that also matches
`conftest.py`, which defines it rather than being one of them.)

That matters if you touch `models/` or add a `CheckConstraint`: your local run can be green because
the tests that would have caught you never executed.

**Bringing the stack up is not enough on its own**, and this is the trap worth knowing. The skip is
a connection attempt against `POSTGRES_SERVER`, which a `src/.env` copied from the template sets to
`db` — the *compose* hostname, which does not resolve on the host. So `docker compose up` followed
by `uv run pytest` on the host still skips every one of them, no matter that Postgres is up and its
port is published. Point the host run at the published port instead:

```bash
POSTGRES_SERVER=localhost ENVIRONMENT=local SECRET_KEY=testsecret uv run pytest -q
```

Inside the workspace `src/.env` already says `localhost`, so the override changes nothing there.

That is the difference between a run ending `… passed, … skipped` and one ending `… passed` with no
skip line at all. **The skip line is the whole signal** — the totals themselves say nothing, since
they move with every test added, and this is the only thing on screen that tells you which of the
two runs you just did. CI sets exactly that variable and fails the job if anything skips (see
below), so this is about getting the answer before you push rather than after — but the skip is
silent and a green local run looks identical either way, so it is easy to spend a review round
believing those tests ran.

**That run does not touch the database the stack serves from.** `tests/conftest.py` appends `_test`
to whatever `POSTGRES_DB` names — `opendive_test` with the stock `src/.env`, and `opendive_test_gw0`
and on, one per worker, under `-n` — creates it on the same server if it isn't there, and brings it
up to `head` with the migrations. So the command above needs the stack up for its Postgres and
nothing more; the dev database keeps its own rows, and the test rows accumulate somewhere
disposable. Dropping them is one statement per database, and the next run rebuilds each from empty:

```bash
docker compose exec db psql -U postgres -Atc "SELECT datname FROM pg_database WHERE datname ~ '^opendive_test(_gw[0-9]+)?$'" \
  | xargs -I{} docker compose exec -T db psql -U postgres -c 'DROP DATABASE "{}"'
```

Against a Postgres on the host, as inside the workspace, run the same two `psql` calls with
`-h localhost` in place of `docker compose exec db` and `exec -T db`.

Worth knowing before you go looking: those databases are migrated, not `create_all`ed, so a model
change you have not yet generated a revision for fails these tests rather than passing them. And a
database left at a revision from a branch you have since left cannot be upgraded from — the suite
says so, names the database, and the drop above clears it.

Alternatively use the containerised suite, where `db` resolves and nothing needs overriding — note
that `docker-compose.test.yml` is an *overlay*, so it has to be passed alongside the base file
rather than on its own:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up --build --abort-on-container-exit api
```

CI does run Postgres and Redis as service containers, and fails the job if *any* test skips — so
unlike before, a green CI run means the database-backed ones actually executed. The run passes
`-rs`, so a job that fails this way names the tests that skipped rather than only counting them.

There is no allowlist: the check is a grep over pytest's counts line, which cannot tell a deliberate
skip from an unreachable database. The suite has no deliberate skips today, and adding the first one
means reworking that step in `.github/workflows/tests.yml` — against the `SKIPPED` lines `-rs`
prints, say — rather than filling in a slot that already exists.

One thing to know when you do run them against a live database: the `create_user` helper commits a
row per test that nothing cleans up afterwards, so expect a scattering of uuid-named users to
accumulate in that `_test` database.

Ruff is configured with `fix = true`, so `uv run ruff check src tests scripts` will repair what it
can on its own, and `uv run ruff format src tests scripts` handles the rest. Line length is 120.
Everything under `app.*` is type-checked with `disallow_untyped_defs` — new functions need
annotations.

The markdown docs are formatted too — drop the `--check` to rewrite them:

```bash
uv run mdformat *.md docs .github tests
```

That covers `DECISIONS.md`, if you append to it. Write the new section however it comes out, run the
command, and it gets wrapped to 100 columns like the rest; no counting characters by hand. The
settings are in `.mdformat.toml`, and the paths are listed explicitly because `mdformat .` would
walk into `.venv/`.

Docstring style follows the numpy convention by habit, not by enforcement: no `D` rules are enabled,
so nothing checks it.

One mypy quirk in `tests/`: `call-arg` is disabled there. Pydantic's mypy plugin doesn't read
defaults out of `Annotated[T, Field(default=None)]`, which is the form every schema in `app/schemas`
uses, so it reports a missing argument for every optional field a test omits — 108 false positives
when the suite was first pointed at mypy, which is what the exemption exists for. Every other error
code still applies. See `DECISIONS.md`.

## How the code is laid out

```
src/app/
  api/v1/      route handlers - request/response only, thin
  crud/        database access (FastCRUD), one module per table
  services/    business logic that doesn't belong in a route
  models/      SQLAlchemy models
  schemas/     Pydantic request/response schemas
  core/        config, security, exceptions, worker, db setup
src/scripts/   one-shot maintenance scripts, run inside the container
scripts/       build-time tooling, run by hand and never shipped
tests/
```

Follow the existing layering: a route validates and authorizes, a service decides, a crud module
talks to the database. If a handler is growing branches, that logic probably wants to be a service.

Tests live in `tests/`, one module per feature area (`test_dives.py`, `test_gear.py`, …). New
endpoints and new parsing behaviour should come with tests; bug fixes should come with a test that
fails without the fix.

## Things that will bite you

**Every schema change ships an Alembic revision.** The API runs `alembic upgrade head` in its
lifespan, so your change reaches a database — yours or a self-hosted instance's — by the API
starting, and CI runs the same upgrade before its tests. Adding a column means:

```bash
cd src && POSTGRES_SERVER=localhost uv run alembic revision --autogenerate -m "add dive.deco_model"
```

`POSTGRES_SERVER=localhost` for the same reason the test suite needs it — `src/.env` names the
compose hostname `db`, which does not resolve on the host — and autogenerate needs a live database
to compare the models against, so the stack has to be up. Read the file it writes before committing
it. Autogenerate compares the models against the database it connects to and is a first draft: it
cannot see a data backfill, it renders a rename as a drop plus an add, and it will happily write
`DROP TABLE` for anything in your database that the models no longer describe. Then
`docker compose restart api` applies it (`src/migrations` is bind-mounted, so the new revision is
already inside the container). Inside the workspace, `cd src && uv run alembic upgrade head` does:
the API's `--reload` does not notice a new file.

CI fails the PR if the models and the revisions disagree — it runs `alembic upgrade head` against an
empty database and then `alembic check`, which autogenerates again and fails on any difference. That
is also the fastest local check that you generated everything you needed:

```bash
cd src && POSTGRES_SERVER=localhost uv run alembic check
```

**A database that predates migrations needs stamping, once.** Anything created by the old
`create_all()` workflow — every dev database from before this landed — has the tables but no
`alembic_version`, so `upgrade head` tries to create them again and the API fails to start. Tell
Alembic it is already current, then verify that claim:

```bash
cd src
POSTGRES_SERVER=localhost uv run alembic stamp head
POSTGRES_SERVER=localhost uv run alembic check
```

The `check` is the part not to skip. The old workflow applied `ALTER TABLE`s by hand from PR
descriptions, so any given machine may be missing a constraint nobody ran, or still carrying a
column that was dropped from the models — a stamp asserts a state nobody verified, and
migrate-on-start will never notice afterwards. Apply whatever `check` reports by hand, or wipe the
database and let the migrations build it: `docker compose down -v && docker compose up`, or, inside
the workspace, drop and recreate the `POSTGRES_DB` database and restart the API.

**Grep [DECISIONS.md](DECISIONS.md) for what you are about to touch.** It records the non-obvious
choices and the traps already hit — the schema-change workflow above, check-constraint behaviour,
the caching and cache-invalidation strategy, auth details — and is far too long to read whole. When
you make a decision the code cannot explain by itself, append a short section as part of your PR:
under 150 words, present tense, what was chosen and why. Most reasons belong in a one-line comment
at the site instead. The auth design, with sequence diagrams for every flow, is in
[docs/authentication.md](docs/authentication.md).

## Adding a format

Every dive-computer file a diver uploads - on the dive form and in logbook import alike - is read by
the [`divejson`](https://github.com/divejson/divejson-py) package, so **a reader is a contribution
to that package, not to this repository**. Its `CONTRIBUTING.md` says how an adapter is written and
what a sample file needs; a real export from a computer nobody on either project owns is the scarce
ingredient.

The one other reader is `divejson-raw`, from [`divejson-c`](https://github.com/divejson/divejson-c),
and it reads the one kind of file nobody uploads: a dive's bytes as the iOS app downloaded them
through libdivecomputer, stored under `libdivecomputer` with the descriptor the app tagged them
with. The api runs it as a subprocess, found on `PATH`; the `Dockerfile` copies it from a pinned
`ghcr.io/divejson/divejson-c` image, and a computer it does not read yet is a contribution to
libdivecomputer or to that repository.

**Its tests need the binary.** Without `divejson-raw` on `PATH` they skip, and the ones that hold
its isolation also need Linux, whose address-space ceiling it runs under. CI installs the binary the
`Dockerfile` pins and fails on the skip. Locally, take it from the same image (Linux only, as the
binary is):

```bash
image=$(sed -nE 's/^FROM (.+) AS divejson-raw$/\1/p' Dockerfile)
docker create --name divejson-raw "$image" \
  && docker cp divejson-raw:/usr/local/bin/divejson-raw ~/.local/bin/ && docker rm divejson-raw
```

or run the containerised suite, whose `test` stage carries it.

What a new format owes this repository, once a `divejson` release carries it and the pin moves, is
three table rows in `src/app/services/dive_reader.py`: the label a diver reads it by
(`FORMAT_LABELS`), the content type a stored file of it is served back as (`FORMAT_CONTENT_TYPES`),
and whether its file is a diver's logbook or one computer's recording (`FORMAT_KINDS`), which
decides where an import of several files reads it. `test_every_read_format_has_a_label` and its
siblings fail the build until all three are there. The format id is written to
`dive_file.parser_key` on every stored file and is part of the data model.

A release that moves either pin - `divejson` in `uv.lock`, or the `divejson-raw` image in the
`Dockerfile` - moves the reader version every stored profile it fed records, so those profiles are
behind until the backfill re-reads them from their stored files - valid meanwhile, and served as the
previous reader left them:

```bash
docker compose exec api python -m src.scripts.backfill_dive_profiles
```

Inside the workspace, the same module runs from the repo root as
`.venv/bin/python -m src.scripts.backfill_dive_profiles`.

A bump of the FIT decoder alone (`fitdecode`) moves no version, so it is followed by the backfill
forced over the FIT files: `--force --parser-key fit`.

## Refreshing the vendored data files

Two files under `src/app/data/` are built from public datasets and checked in, so that a self-hoster
needs no account, no key and no outbound call for the features that read them:

| File                         | Built by                          | Upstream                          | Licence       |
| ---------------------------- | --------------------------------- | --------------------------------- | ------------- |
| `commons_dive_sites.geojson` | `scripts/build_commons_bundle.py` | OpenDiving Commons, Natural Earth | **ODbL 1.0**  |
| `marine_areas.geojson`       | `scripts/build_marine_areas.py`   | Natural Earth                     | public domain |

Both are run **by hand, and by nothing else** — no CI job, no Makefile target, no schedule:

```bash
export ENVIRONMENT=local SECRET_KEY=any-value-but-a-placeholder   # the Commons builder imports the app's match rule
uv run python scripts/build_commons_bundle.py /path/to/opendiving-commons   # a clean checkout of its main
uv run python scripts/build_marine_areas.py
```

Each writes its output atomically (staged to `.tmp`, then `os.replace`d), because the running API
reads `./src/app` live — `docker-compose.yml` bind-mounts it into the container, and inside the
workspace the API runs from it — and a plain in-place write would let a process read a half-written
file.

**`marine_areas.geojson` refreshes when the file's meaning changes, in the PR that changes it**, not
on a clock. **The Commons bundle is re-run from the repository's `main` before every release**: that
re-run is how a self-hosted copy of the Commons is refreshed on upgrade, the instance's startup sync
bringing its rows in line with the file.

**A batch from an open dataset enters through the same file.** An importer beside the builder reads
the publisher's data, leaves out what it doubts, and writes candidates, which the builder takes
beside the checkout:

```bash
uv run python scripts/import_noaa_florida_keys.py keys.geojson
uv run python scripts/import_noaa_thunder_bay.py thunder-bay.geojson
uv run python scripts/import_fwc_artificial_reefs.py vessels.geojson
uv run python scripts/build_commons_bundle.py /path/to/opendiving-commons \
  --batch keys.geojson --batch thunder-bay.geojson --batch vessels.geojson
```

`scripts/import_osm_wrecks.py` is the same, with the checkout as its first argument, since it reads
the pool to leave out what it already holds. Files are judged in the order named, so the first to
claim a site keeps it. The candidates file is not checked in: the pull request's diff of the bundle
is the batch, one line per record, and its body gives the count kept, the count left out by each
rule, and the names held. Read it as a diff before merging, because nothing removes a Commons record
once the hosted instance has inserted it. **A release's rebuild waits for the publish.** Until the
repository's daily publish has copied a merged batch, a rebuild from its `main` would lose those
records, and the builder fails and writes nothing rather than drop them.

**A further source starts in the repository.** Its token, identifier form and licence go there
first, because the daily publish fails on a record of a source it does not know. An importer then
goes in `scripts/`, on `curated_source.py`, with `sources` and `_ATTRIBUTION` in the builder
crediting the token and the token in `CURATED_SOURCES` in `services/commons_records.py`, and the
batch follows through the steps above. No migration: a Commons row names no source. A CC BY source
waits for its publisher's waiver, since the file's ODbL would hand a reuser a permission CC BY does
not.

**The record count is pinned and the test will tell you.** `tests/test_commons_bundle.py` asserts
the exact number of records, so a refresh that moves it fails there rather than landing quietly.
That is the point — a shrinking pool is usually a broken reader, not the world losing dive sites.
When the change is genuine, update the constant there and the count in `DECISIONS.md`, and say in
the PR what moved and why. `marine_areas.geojson` has the same guard at 293 features.

The bundle is an **ODbL Derivative Database**. Its notice block — the commit it was built from, the
licences and the per-source attribution the API serves with every record — lives inside the file,
not beside it, because ODbL §4.2 requires the notice to travel within the data or its metadata. Do
not strip it, and do not add records from a source whose terms are non-commercial or no-derivatives:
the file is ODbL as a whole and cannot carry them.

## Pull requests

- **Your commits do not need to be signed.** PRs here are squash-merged, and GitHub creates and
  signs that one commit with its own key, so what a self-hoster audits on `main` later is signed
  whatever your branch carried. Sign if you already sign — nobody will ask you to set GPG up for a
  pull request.
- Branch off `main`, keep the PR focused on one thing.
- **Your branch never needs to be current with `main`.** Once a PR is green and reviewed, a
  maintainer adds it to the merge queue, which squashes it onto `main` as it then stands, runs the
  required checks on that result and lands it. Rebase only to resolve a conflict.
- Title the PR as a conventional commit — `<type>[(scope)][!]: <description>`, e.g.
  `feat: store the dive-computer export a dive was imported from`. Types: `feat`, `fix`, `refactor`,
  `docs`, `test`, `chore`, `perf`, `ci`, `build`, `revert`. A CI check enforces this, and re-runs
  when you edit the title, so a rejected PR needs no new commit. PRs are squash-merged, so the title
  becomes the commit subject on `main` — commits within your branch can say whatever is useful while
  working. (Older history mixes prefixed and plain subjects; new PRs need the prefix.) The same
  workflow puts the type on the PR as a label, which is what files it under the right heading when a
  release's notes are generated — so a retitle is all it takes to move it.
- Say in the description what you changed, why, and anything a reviewer has to do by hand (schema
  SQL, new env vars, a backfill script).
- If the change affects the API contract, mention whether the web or iOS client needs a matching
  change — and if you are writing that matching change yourself, see below.

**Changes that span both repos.** Open the api pull request first, and link the two to each other so
whoever lands on one can see the other half. Expect the api side to merge first: a client is written
against an endpoint that exists, and merging in the other order leaves `main` calling something that
isn't there yet.

No CI job tests the pair together. Each repo's checks run against its own tree, so a contract that
drifted between them passes both and breaks only when the two are actually run side by side — which
makes the linked pull requests the whole safety net, and the linking an ask rather than a courtesy.

## What to expect

One maintainer, on this in their spare time. It is the same caveat [SECURITY.md](SECURITY.md) makes
about vulnerability reports, and it applies to ordinary issues and pull requests too — there is no
review rota and nobody covering a quiet fortnight.

The usual shape is a first response within a few days. If a week goes by in silence, **ping the
thread.** That means it got buried, not that it was read and dismissed, and a reminder is welcome
rather than pushy. Nothing here is closed for going stale either: an issue or a PR gets closed by a
human with a reason, or it stays open.

## AI-assisted contributions

Welcome, and this repository is unusually ready for them on purpose. [AGENTS.md](AGENTS.md) and
[DECISIONS.md](DECISIONS.md) are written for your tools as much as for you — the first is the
conventions an agent should have loaded before it writes a line, the second is every trap already
hit here and what it cost. Point a coding agent at this repo and it starts better briefed than it
would on most.

What that does not change is who owns the pull request. The person who opens it is its author: you
ran the change, you understood it, and you can answer a review question about any line without going
back to the model to ask. That is the entire bar, and it is the same one a hand-typed PR meets.

Bulk submissions nobody read before opening get closed without ceremony — not as a verdict on the
tooling, but because review attention is the scarce resource here and unreviewed output is what
spends it fastest.

## For maintainers

Commits pushed to this repository's own branches are signed, and two hooks keep them that way. Both
ship with the repository and neither switches itself on. Once per clone:

```bash
git config core.hooksPath .githooks
```

`.githooks/pre-push` then refuses to push a commit carrying no signature at all. Linked worktrees
share the setting, so that single command covers them too.

The second is `.claude/hooks/no-unsigned-commits.py`, which stops a Claude Code session running a
command that switches signing off — earlier than the push hook, while there is still nothing to
rewrite. The script is committed; what registers it is not, so paste this into
`.claude/settings.local.json`, which is untracked and yours:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/no-unsigned-commits.py"
          }
        ]
      }
    ]
  }
}
```

That file already exists in most clones — merge the `hooks` key in rather than overwriting it. And
unlike `core.hooksPath`, this one does **not** reach linked worktrees: `git worktree add` copies
nothing untracked, so a worktree gets the script with nothing wired to it and the push hook is the
only guard there. Paste it again per worktree if you want the earlier one back.

Both instructions live here rather than in *Getting set up* because neither is a contributor's
problem: the commits in a pull request do not need to be signed, and enabling the push hook in a
clone that does not sign only walls you out of your own push.

## Cutting a release

**A release here is a component release**: the `opendiving-api` image, and notes recording what went
into it. The *product* release — the version both images are tagged with, plus the three files that
install them — is cut in [opendiving/opendiving](https://github.com/opendiving/opendiving), last of
the three, and
[its CONTRIBUTING.md](https://github.com/opendiving/opendiving/blob/main/CONTRIBUTING.md) is the
ritual as a whole. This section is the api half of it.

Releases are cut deliberately, never minted per merge. A version is an event self-hosters read
before they pull, and a stream of releases whose notes are one PR title each trains people onto
`latest` — the tag you least want someone following. Accumulate until the window tells a coherent
story, until a fix somebody is waiting on lands, or until anything needs a pinnable reference:
pre-launch that means "whenever useful", after launch expect every one to four weeks.

**A merge to `main` is not a release, but it is a deploy.** Every one publishes `:edge` and
`:sha-<12>` and then calls the project's own instance's deploy hooks with that build's digest, so
the hosted instance runs `main` continuously — the ordinary meaning of "merged". Nothing a
self-hoster pins moves: no `:latest`, no `X.Y.Z`, no `X.Y`, and no release. Two consequences worth
knowing before you merge. **Migrations are fix-forward from here on**: `main` reaches a database
holding real data within minutes, so a revision that turns out to be wrong is corrected by a
follow-up revision, never by a rewrite of the one that shipped. And an `:edge` image reports the
same `version` as every other `:edge` image, since the manifest only moves at step 1 below — which
is why `/api/v1/health` also reports `commit`, baked into the image by the build. That field is what
identifies a running build; `version` on its own no longer does.

> **The deploy half needs a repository secret set once, by hand, and until it is, every merge
> publishes and deploys nothing.** `RENDER_DEPLOY_HOOKS` holds this image's Render Deploy Hook URLs,
> comma-separated — one per service, so `api` and `worker` are two — with newlines accepted as
> separators too. Copy each from that service's *Settings → Deploy Hook* in the Render dashboard.
> Unset, the `deploy` job prints a notice and exits **zero**, because the secret is absent on every
> fork and a red X on an outside contributor's merge is how a check stops being read — so nothing
> tells you it has not been done, which is why it is written here. A hook that is present and fails
> *is* red, and re-running that job alone is the whole fix: the image is already published.

Versions move in lockstep across all three repositories — this one,
[opendiving-web](https://github.com/opendiving/opendiving-web) and the product repository: one
product version, so `opendiving-api:0.4.0`, `opendiving-web:0.4.0` and release `v0.4.0` over there
are always a matched set. That is also why the only release tool that runs is the product
repository's own coordinator — semantic-release and release-please both compute a version per repo
from that repo's own commits, which drifts apart on the first api-only fix and then has to be forced
back by hand at every release afterwards. Neither is used, and nothing in *this* repository decides
a version: one workflow next door reads all three windows and writes one number into two
repositories.

**Pick the number** by looking at all three repos' windows together:

| The window contains                                                                            | Pre-1.0 | From 1.0.0 |
| ---------------------------------------------------------------------------------------------- | ------- | ---------- |
| Anything breaking — a changed config or env contract, removed behaviour, a manual upgrade step | minor   | major      |
| Any user-visible feature                                                                       | minor   | minor      |
| Fixes and internals only                                                                       | patch   | patch      |

"Breaking" is about the operator's experience, not the code's — which is why a schema change on its
own is *not* breaking: migrations run themselves on startup, and the upgrade drill is what keeps
that claim honest. What counts is anything the operator has to do by hand before the new version
will run: an edited `.env`, a changed config contract, a removed behaviour they depended on.

Every PR title is a conventional commit subject, so the breaking half of that table has a scanner:

```bash
git log --format=%s v0.3.0..main | grep -E '^[a-z]+(\([^)]+\))?!:'
```

Then run
**[Cut the release](https://github.com/opendiving/opendiving/actions/workflows/release-cut.yml)**
from the product repository's Actions tab, on `main`. One dispatch cuts the whole product release,
and
[that repository's CONTRIBUTING.md](https://github.com/opendiving/opendiving/blob/main/CONTRIBUTING.md)
documents the two boxes it takes. **Nothing here is bumped or tagged by hand** — an instruction
anywhere to do either describes the ritual this replaced. What the dispatch does to *this*
repository is:

1. It opens a pull request titled `chore(release): v0.4.0` writing `0.4.0` into `pyproject.toml`
   **and** into `uv.lock`, waits for the required checks, and squash-merges it. Both files carry the
   number: the lockfile records this project's own version in its
   `[[package]] name = "opendiving-api"` block, and the `Dockerfile` installs with
   `uv sync --locked`, so a bump that wrote the manifest alone would produce a pull request
   `runtime-imports` can never let merge. Nothing else here declares one — the API reads its own
   from the installed package metadata, and the product repository has no manifest to bump on
   purpose, the check that matters there being "do both images exist at this version", which is
   stronger than any local record of what the version is supposed to be.

2. It tags the squash commit `v0.4.0`. A hand bump or a stray tag is how the *next* release gets
   stuck rather than this one: the version decision refuses when the entries that declare a version
   disagree, or when a repository's newest tag is not what its manifests say, and either of those is
   exactly what hand work leaves behind.

3. The tag push runs **Publish Image**, which builds amd64 and arm64 on native runners and pushes
   `0.4.0`, `0.4`, `latest` and `sha-<12>` — plus the bare major (`1`, `2`, …) once this is past
   1.0.0, which is withheld below it because a `0` alias would read as "any 0.x". The tag has to be
   exactly `vX.Y.Z`: pre-releases and other shapes have no alias story here and are refused. A tag
   whose name disagrees with the manifest version is refused the same way, before anything is built
   — so nothing was published, and the fix is to correct the bump and re-cut the tag.

4. The same run **publishes** this repository's component release, with generated notes and **no
   assets** — the install files are attached by the product repository's release, which is the one
   an operator downloads from. Nobody writes a headline here and nobody presses publish. The release
   somebody reads before deciding whether to upgrade is the product one, and that is where the
   **Breaking** section is written by hand: say "None" in so many words when it is empty, because
   generated notes omit an empty category and silence is not an answer. A change an existing install
   has to copy into its own `.env` or compose file — a new required variable, a new service —
   belongs in that section, since `docker compose pull` does not update the compose file.

5. Once both images are green the coordinator tags the product repository last, whose **Release**
   workflow checks that `ghcr.io/opendiving/opendiving-api:0.4.0` and
   `ghcr.io/opendiving/opendiving-web:0.4.0` both exist with both architectures and refuses to
   publish anything if either is missing — the check that replaced the by-eye "is the web image
   there?" step this section used to carry, and the one no per-repo workflow can make. It leaves a
   draft, and a person finishes it.

`v0.1.0` was cut by hand on 2026-09-12, before the coordinator existed, and it is the only release
this repository has so far. It had the quirk only a first release has: with no earlier tag to
generate notes against, `--generate-notes` had no floor and enumerated the entire history, which was
trimmed by hand. Every release after it has a predecessor and needs none of that — which is part of
why these notes can go out generated rather than reviewed.

**A published version is never repointed.** A bad release gets a successor, not a rewrite.
Immutability starts at *publish*, so a tag whose build failed before pushing anything published
nothing; deleting and re-cutting such a tag is an organisation admin's operation, since the `tags`
ruleset blocks deletion and force-pushes on `refs/tags/v*` for everybody else. The one sanctioned
reason to rebuild a released version is a CVE in a base image: run **Publish Image** by hand with
`ref` set to the `v` tag, and tick **Also push :latest** if that version is still the newest. One
run recomputes the version's whole alias set, which is the point — a hand-picked subset would leave
everyone following `latest` or `0.4` on the vulnerable digest. What tells you there is a CVE to
rebuild for is the next section.

**What publishing this release rather than drafting it costs.** It goes out minutes before the
product repository's guard runs, so a guard failure — one image missing, or half-published — leaves
a published component release here for a product version that never released, and nothing can
un-publish the tag it names. That is the existing window widened rather than a new one: both images,
`latest` included, were already pushed by then, and what the guard protects is the product release
and the install assets, never the images. The genuinely free-to-delete guard is the tag↔manifest
check in step 3, which runs before anything is built.

## Staying on top of CVEs

The rebuild above is the *response*. Two things are wired up to raise the alarm in the first place,
and they watch different objects — neither substitutes for the other.

**The published images.** `.github/workflows/vulnerability-scan.yml` scans two things every morning
with Trivy. `edge`, which is what the project's own instance is running; and the newest release —
its `X.Y.Z`, `X.Y`, bare major and `latest`, which are one image under four names, resolved to a
digest so it is scanned once. Both are in the set unconditionally, which is why `edge` is reached
before the "nothing released yet" skip rather than after it: until the first tag is cut, `edge` is
the only image that exists. It reports HIGH and CRITICAL findings in both halves of each image: the
Debian packages that come from `python:3.15-slim-trixie`, and the Python distributions `uv`
installed. This is the job that closes the loop with the paragraph above, because the case it
catches is a release that was clean the day it shipped and grew a CVE three weeks later, with no PR
in flight and nobody looking.

**The alert is a code-scanning alert** on the repository's **Security** tab, and you are the one who
acts on it. The run also writes a summary to the workflow run itself, and that is the half worth
reading: it splits the findings by what actually fixes them, because that differs by image as well
as by package:

- **Anything on `edge`** — merge the bump. That is the whole remedy: the next build on `main`
  republishes `edge` from the current base image and against the current `uv.lock`, and redeploys
  it. No release is involved.
- **OS package on a released alias** — the rebuild above, dispatched at the `v` tag the summary
  names. One dispatch recomputes every release alias the scan covers, which is why the scan covers
  exactly those and no more. It does not touch `edge`, which a merge has already dealt with.
- **Python package on a released alias** — *not* fixable by a rebuild at any tag. That version comes
  from the `uv.lock` committed at the tag, and the rebuild checks that tag out and runs
  `uv sync --locked` against it, so it reinstalls the identical version no matter how many bumps
  have since landed on `main`. Merge the bump and **cut a new patch release** — the ordinary flow
  above, not the in-place rebuild.

Every scan replaces the whole alert set, so a CVE that takes upstream a fortnight to patch is one
alert for a fortnight rather than fourteen notifications, and an alert closes itself on the first
scan that no longer finds it — you never close one by hand to say it is fixed. Only findings with a
published fix become alerts; the ones upstream has not fixed are a count in the run summary, because
no rebuild collects them and an alert you cannot act on is noise.

Until 2026-09-12 this alert was a GitHub issue labelled `image-cve` instead, because SARIF upload
was a paid feature on a private repository. Deleting that step left the last such issue with nothing
to update or close it, so it was closed by hand the same evening; none has been filed under that
label since. Anything it reported that is still present comes back as an alert on the next scan —
the `published-images` job runs on the schedule and on `workflow_dispatch` and nowhere else, so that
is the daily `0 6 * * *` (which GitHub starts late) or a dispatch now.

**Proposed changes.** The same workflow runs a second, much cheaper job on every PR — Trivy over
`uv.lock`, no image built — which *fails the check* on a HIGH or CRITICAL that has a fix available.
That is about a change you are proposing rather than about what is deployed, so it stays out of the
alerts above. It ignores findings with no fix published, because there is no move to make on those.

**Version bumps.** `.github/renovate.json5` is the other half: it watches `uv.lock` and
`pyproject.toml`, both `Dockerfile` base images, the development compose file's third-party images,
and every pinned GitHub Action. Routine updates arrive in one batch on Monday morning; a
vulnerability-driven one ignores the schedule and is titled `fix(deps):`, so it lands in the Fixes
section of the release notes rather than among the chores. The install bundle's own digests are
renewed by the product repository's Renovate config, not by this one.

> **Renovate is running, and getting it there took a one-time step by hand that nothing in this
> repository could have done.** The Mend-hosted
> [Renovate GitHub App](https://github.com/apps/renovate) is installed on the `opendiving` org; it
> reads `.github/renovate.json5` on its next run and needs no further setup, and the **Dependency
> Dashboard** issue it keeps open here is where it reports — every update it is holding back, and
> every one you have told it to ignore. The installation names its repositories one at a time rather
> than covering the whole org, so a **repository added later has to be added to it**; nothing warns
> you that a new repository's config file is being read by nobody, which is why this is written
> down.

One thing Renovate will not do on its own is move Python. `requires-python` in `pyproject.toml`,
ruff's `target-version`, `.python-version` and the two `Dockerfile` base tags all have to move
together, and Renovate can only reach the last three — an auto-opened PR would be wrong by
construction. So that update is grouped and held behind a checkbox on the **Dependency Dashboard**
issue: it tells you 3.16 exists and waits for a person.

**What none of this watches**, stated so it is not mistaken for coverage:

- **Any release but the newest.** This is not a gap so much as the scan agreeing with
  [SECURITY.md](SECURITY.md), whose supported-versions table is "the most recent release: yes;
  anything older: no — upgrade to the newest". A dispatch only ever repoints the aliases of the
  version it names, so scanning `0.2` would produce an alert with no supported move attached,
  recurring forever. The four release aliases that *are* scanned — `X.Y.Z`, `X.Y`, the bare major
  and `latest` — are every form in which someone can be pinned to the supported release, which is
  why SECURITY.md can promise that `docker compose pull` is the whole fix for a base-image CVE even
  with a version pinned. If an old minor ever does have to be rebuilt, it is a dispatch at its own
  tag. `edge` is scanned alongside them and is not one of these forms: nobody is meant to be pinned
  to it, and it exists in the set because the project's own instance runs it.
- **The `linux/arm64` image**, on the assumption that it installs the same Debian packages as
  `linux/amd64`. If that ever stops holding, the scan step is where a `--platform` pass goes.
- **Vulnerabilities with no fix published upstream.** They are counted in the run summary but drive
  nothing and never become an alert, since no rebuild collects a package that does not exist.

## License

By contributing you agree that your work is licensed under [AGPL-3.0](LICENSE), same as the rest of
the project.
