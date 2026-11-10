<!--
Maintainer note. Block-level HTML comments are stripped before this file enters Claude's
context, so this costs nothing to keep here.

The project instructions live in AGENTS.md, imported below, so every coding agent reads one
file instead of a copy that drifts. Claude Code does not read AGENTS.md on its own - the
import is what loads it. Only genuinely Claude-specific instructions belong beneath it. See
"The project instructions live in AGENTS.md" in DECISIONS.md.
-->

@AGENTS.md

## Running the tests

`CONTRIBUTING.md` is the single source of truth for the commands. Two caveats it states that are
easy to miss, both of which make a run *look* clean while proving nothing:

- **mypy runs as more than one invocation** — `CONTRIBUTING.md` says how many and why. `ENVIRONMENT`
  and `SECRET_KEY` must be set.
- **The Postgres-backed tests skip themselves** unless `POSTGRES_SERVER` names a host the *test
  process* can reach. Inside the workspace `src/.env` sets it to `localhost`, so they run with no
  override. On the Docker path `src/.env` sets it to the compose hostname `db`, so a run on the host
  skips every one of them even with the stack up and the port published; `POSTGRES_SERVER=localhost`
  is what makes them run **in a checkout that has `src/.env`**. A worktree doesn't: the file is
  gitignored, so it never follows a `git worktree add`, and the credentials fall back to
  `postgres`/`postgres` and database `postgres`. The Docker stack uses its own, so the override
  alone then fails on those instead of on the host and the tests skip identically — copy `src/.env`
  in first. The workspace's Homebrew Postgres accepts the fallbacks, so there a worktree's suite
  runs anyway, against `postgres_test` instead of `opendive_test`. CI already does this and fails if
  they skip, so the gap is local feedback, not merge safety.

Running them is safe for the dev stack: the suite gives itself a database — `POSTGRES_DB` with
`_test` appended, created and migrated by `tests/conftest.py` — and never writes to the one the dev
API serves from. It did until 2026-08-29, and the damage was not visible until the `api` container
next restarted and died in its startup migration. See *"The suite has its own database, and builds
it with the migrations"* in `DECISIONS.md` before changing how the fixtures reach Postgres.

## Testing in the browser

This repo rarely needs a browser: the admin panel, the OpenAPI docs at http://localhost:8000/docs, a
magic link or an OAuth redirect, or checking an endpoint's effect in the web app on
http://localhost:3000.

When it does, use the **ego-browser** skill (`/ego-browser`) by default: run `ego-browser skill`
first and follow what it prints, since its API is its own, not Playwright's. It is user scope
(`~/.claude/skills/ego-browser/`), so a clone may not have it; then fall back to the `playwright`
MCP server.

- **Use the running dev stack**: the API on :8000 and the web app on :3000. Check with `curl` before
  starting anything, and never start a second API or `next dev`.
- **It drives the user's own Ego Lite profile**, so the web app is often already signed in as them:
  what you save lands in their account, and signing in as another account replaces their session in
  that profile, so ask first.
