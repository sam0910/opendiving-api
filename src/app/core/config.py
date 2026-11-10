import logging
import os
import re
import warnings
from enum import Enum, StrEnum
from importlib import metadata
from typing import Self
from urllib.parse import quote, urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.config import Config

current_file_dir = os.path.dirname(os.path.realpath(__file__))
env_path = os.path.join(current_file_dir, "..", "..", ".env")
config = Config(env_path)


def split_csv(raw: str | None) -> list[str]:
    """Parse one of this app's comma-separated list settings.

    Several settings are lists of addresses or networks, and every one of them is typed
    `str | None` rather than `list[str]`: for a complex field type pydantic-settings
    parses the environment variable itself and expects JSON, so `TRUSTED_PROXY_IPS=
    172.16.0.0/12` fails validation at startup no matter what `cast=` does at the field.
    Shared so all of them agree on what "empty" and "spaces after the comma" mean.
    """
    return [entry.strip() for entry in (raw or "").split(",") if entry.strip()]


def _installed_version() -> str | None:
    """This distribution's version, from `pyproject.toml` by way of the installed
    metadata.

    `APP_VERSION` is not in `src/.env.example`: a copy there would make a released image
    report whatever version the operator's own `.env` happened to carry - the template's,
    usually, frozen at whenever they copied it. Reading it here makes
    `pyproject.toml` the single source, and the setting an override rather than the
    definition. It reaches `/api/v1/health`, the JSON export's `generator` block and the
    UDDF `<version>` element, all of which are how someone reports a bug against a
    specific build.

    `None` when the app is run from a source tree that was never installed - the same
    thing an unset `APP_VERSION` produced before, and every consumer already handles it.
    """
    try:
        return metadata.version("opendiving-api")
    except metadata.PackageNotFoundError:
        return None


class AppSettings(BaseSettings):
    APP_NAME: str = config("APP_NAME", default="OpenDiving")
    APP_DESCRIPTION: str | None = config("APP_DESCRIPTION", default=None)
    APP_VERSION: str | None = config("APP_VERSION", default=_installed_version())
    # The commit the running image was built from, baked in by the `Dockerfile`'s
    # `APP_COMMIT` build arg. `None` everywhere else - a source checkout, a locally built
    # image. Two consumers: `/api/v1/health` reports it, and `core/utils/cache.py` names the
    # response cache's keys after it, so a deploy cannot be served the previous build's
    # response bodies - and is consulted first there because it is the only build identity
    # that also moves when a *dependency* does. Where the arg was not passed, that module
    # falls back to a digest of its own sources rather than to `APP_VERSION`, which moves per
    # release and so cannot tell two commits apart. It exists because `APP_VERSION`
    # stops identifying a build the moment images are published per merge rather than per
    # release: on that channel every one of them reports the manifest's version, and the
    # AGPL source offer has to name something an operator can check out. Deliberately not
    # in `src/.env.example`: it is a property of the image, not of the operator's config,
    # and the one thing `APP_VERSION` proved is that a build identity in a template is a
    # build identity frozen at whenever somebody copied it.
    APP_COMMIT: str | None = config("APP_COMMIT", default=None)
    LICENSE_NAME: str | None = config("LICENSE", default=None)
    # OpenAPI document metadata only ("who maintains this API", published at
    # `/api/v1/openapi.json` on every instance, `production` included) - *not* where the
    # frontend's support form delivers to. That's `ContactSettings.CONTACT_FORM_EMAIL` below.
    CONTACT_NAME: str | None = config("CONTACT_NAME", default=None)
    CONTACT_EMAIL: str | None = config("CONTACT_EMAIL", default=None)


# `SECRET_KEY` values that stand for "I have not set this yet": the one `src/.env.example`
# ships, and the stock stand-ins people type in its place. Every one of them is a published
# string, so it signs *anybody's* access, refresh and onboarding tokens - which is not a
# weak key, it is no key at all. Treated exactly like `LEGACY_DEFAULT_ADMIN_PASSWORD` below,
# and matched case-insensitively after stripping.
#
# `change-me-openssl-rand-hex-32` is published in two repositories, not one: the front
# door's `example.env` ships that exact value too, and an operator's `.env` is a copy of
# *that* file. Which is why `_reject_placeholder_secret_key` names `.env` rather than
# either template - see its docstring.
#
# An explicit list rather than an entropy heuristic on purpose: the failure this guards is
# "the template's own value reached a deployment", not "the operator chose badly", and a
# heuristic that rejects a key someone genuinely generated is a worse bug than the one it
# prevents. CI's key is deliberately not in here: CI boots as `local`, so `CI_SECRET_KEY`
# below is refused only outside it, by the same validator and with the same message.
PLACEHOLDER_SECRET_KEYS = frozenset(
    {
        "change-me-openssl-rand-hex-32",
        "change-me",
        "changeme",
        "changethis",
        "secret",
        "your-secret-key",
        "your_secret_key",
    }
)

# CI's `SECRET_KEY`, published in `.github/workflows/tests.yml` and `CONTRIBUTING.md`.
CI_SECRET_KEY = "test-secret-key-for-testing-only"


MIN_REFRESH_TOKEN_REUSE_GRACE_SECONDS = 5


class CryptSettings(BaseSettings):
    SECRET_KEY: SecretStr = config("SECRET_KEY", cast=SecretStr)
    ALGORITHM: str = config("ALGORITHM", default="HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = config("ACCESS_TOKEN_EXPIRE_MINUTES", default=30)
    REFRESH_TOKEN_EXPIRE_DAYS: int = config("REFRESH_TOKEN_EXPIRE_DAYS", default=7)
    # The same sliding window for a session that asked for `idle_timeout: extended` at
    # sign-in - a native client, which has no tab to sign in again from and may sit unused
    # for weeks between trips. Both windows drive the row's `expires_at`, the cookie's
    # `Max-Age` and the refresh token's `exp` together. Also how long an `Idempotency-Key`
    # hold is kept (`purge_expired_idempotency_holds`).
    EXTENDED_REFRESH_TOKEN_EXPIRE_DAYS: int = config("EXTENDED_REFRESH_TOKEN_EXPIRE_DAYS", default=90)
    # How long after a refresh token is spent a second presentation of it is answered with
    # the pair the first one minted, rather than ending the session as a replay: a reply
    # lost on a bad connection and retried. Held under a minute so that a stolen cookie
    # replayed later still ends the session (`api.v1.auth._handle_revoked_refresh`).
    REFRESH_TOKEN_REUSE_GRACE_SECONDS: int = config("REFRESH_TOKEN_REUSE_GRACE_SECONDS", default=60)
    # How long a temporary, post-verification-but-pre-account "onboarding" JWT (see
    # `create_onboarding_token`/`verify_onboarding_token` in `core.security`) stays
    # valid for. Deliberately short - it exists only to carry a verified identity from
    # `/auth/email/verify` or `/auth/google` to `/auth/complete`.
    ONBOARDING_TOKEN_EXPIRE_MINUTES: int = config("ONBOARDING_TOKEN_EXPIRE_MINUTES", default=30)
    # How long the token minted by `POST /dive/parse` stays usable for attaching the
    # parsed file to a dive (see `create_dive_file_token` in `core.security`). Much
    # longer than the tokens above because it is a staleness bound, not a credential
    # lifetime: a diver may import a file and then spend an evening filling in sites,
    # gear and notes before saving. It grants nothing beyond storing bytes this server
    # already parsed for that same user.
    DIVE_FILE_TOKEN_EXPIRE_MINUTES: int = config("DIVE_FILE_TOKEN_EXPIRE_MINUTES", default=1440)

    # The refresh cookie is `Secure` everywhere it should be, which is everywhere reached
    # over HTTPS. The escape hatch exists for the one deployment shape that cannot have
    # that - an instance on a LAN address with no certificate - where a `Secure` cookie is
    # simply dropped by the browser and the symptom is "signed out on every reload" with
    # nothing in any log. Default `true`, so turning it off is a decision someone makes.
    AUTH_COOKIE_SECURE: bool = config("AUTH_COOKIE_SECURE", default=True)

    @field_validator("REFRESH_TOKEN_REUSE_GRACE_SECONDS")
    @classmethod
    def _grace_above_the_rotation_race(cls, value: int) -> int:
        """The grace is also the line past which a spent token revokes its session, so a
        value at or near zero would sign a diver out for two tabs refreshing at once. Five
        seconds is three orders of magnitude above that race."""
        if value < MIN_REFRESH_TOKEN_REUSE_GRACE_SECONDS:
            raise ValueError(
                f"REFRESH_TOKEN_REUSE_GRACE_SECONDS is {value}; below "
                f"{MIN_REFRESH_TOKEN_REUSE_GRACE_SECONDS} a browser's two tabs refreshing at once "
                "would sign the diver out as a stolen cookie. It cannot be switched off."
            )
        return value


class DatabaseSettings(BaseSettings):
    # The API brings the schema up to `head` on startup, so upgrading an instance is
    # `docker compose pull && docker compose up -d` and nothing else. Off is for operators
    # who want to run `alembic upgrade head` themselves - during a backup window, or from a
    # one-shot container - and it is the same escape hatch Miniflux ships as
    # `RUN_MIGRATIONS`. Turning it off does *not* fall back to anything: the app then boots
    # against whatever schema it finds, and a version behind on migrations fails at the
    # first query that needs the new column.
    MIGRATE_ON_START: bool = config("MIGRATE_ON_START", default=True)


def postgres_uri(user: str, password: str, server: str, port: int, database: str) -> str:
    """The `user:password@host:port/db` half of the DSN, with the credentials
    percent-encoded.

    They are user-chosen strings dropped into a URL, so a password containing `@`, `/`,
    `:` or `#` - all of them ordinary in a generated password, and `@` is what most
    generators reach for first - silently produces a different DSN than the operator
    wrote. `p@ss@db:5432/opendive` parses with `p` as the user and `ss` as the host, and
    the resulting error names a host nobody configured.
    """
    return f"{quote(user, safe='')}:{quote(password, safe='')}@{server}:{port}/{database}"


class PostgresSettings(DatabaseSettings):
    POSTGRES_USER: str = config("POSTGRES_USER", default="postgres")
    POSTGRES_PASSWORD: SecretStr = SecretStr(config("POSTGRES_PASSWORD", default="postgres"))
    POSTGRES_SERVER: str = config("POSTGRES_SERVER", default="localhost")
    POSTGRES_PORT: int = config("POSTGRES_PORT", default=5432)
    POSTGRES_DB: str = config("POSTGRES_DB", default="postgres")
    POSTGRES_SYNC_PREFIX: str = config("POSTGRES_SYNC_PREFIX", default="postgresql://")
    POSTGRES_ASYNC_PREFIX: str = config("POSTGRES_ASYNC_PREFIX", default="postgresql+asyncpg://")

    @property
    def POSTGRES_URI(self) -> SecretStr:
        """Built from the parts and never settable itself, so it cannot disagree with them -
        a set DSN would also outrun the test suite's `POSTGRES_DB` redirect. A `SecretStr`
        like the password it embeds."""
        return SecretStr(
            postgres_uri(
                self.POSTGRES_USER,
                self.POSTGRES_PASSWORD.get_secret_value(),
                self.POSTGRES_SERVER,
                self.POSTGRES_PORT,
                self.POSTGRES_DB,
            )
        )


# The password the upstream boilerplate shipped as `ADMIN_PASSWORD`'s default. It is
# published in this repo's history, so it is treated as "no password at all" rather
# than as a credential - see `Settings._reject_insecure_admin_config`.
LEGACY_DEFAULT_ADMIN_PASSWORD = "!Ch4ng3Th1sP4ssW0rd!"

# `ADMIN_PASSWORD` values that are published strings rather than credentials: the one above,
# the one both config templates ship commented out under an `ADMIN_USERNAME` that defaults
# to `admin`, and the stock stand-ins. Lowercase, matched case-insensitively after stripping.
PLACEHOLDER_ADMIN_PASSWORDS = frozenset(
    {
        LEGACY_DEFAULT_ADMIN_PASSWORD.lower(),
        "a-real-password-not-this-one",
        "change-me",
        "changeme",
        "changethis",
        "admin",
        "password",
    }
)


class FirstUserSettings(BaseSettings):
    ADMIN_USERNAME: str = config("ADMIN_USERNAME", default="admin")
    # No default: the admin panel grants full create/update/delete over every model
    # (see `admin.views`), so an unset password must mean "no admin account", never
    # "a well-known one". `admin.initialize.create_admin_interface` skips
    # `initial_admin` entirely when this is `None`.
    ADMIN_PASSWORD: SecretStr | None = config("ADMIN_PASSWORD", default=None, cast=SecretStr)


class GoogleAuthSettings(BaseSettings):
    # OAuth 2.0 client ID from the Google Cloud Console, shared with the frontend
    # (`NEXT_PUBLIC_GOOGLE_CLIENT_ID`) - it's used there to build the authorization URL
    # the visitor is sent to, and here both to exchange the code that comes back and, as
    # the expected `aud` claim, to verify that the resulting ID token belongs to this app
    # rather than some other Google OAuth client. Not a secret; it rides in a URL the
    # visitor can read.
    GOOGLE_CLIENT_ID: str | None = config("GOOGLE_CLIENT_ID", default=None)

    # The other half of that client, and the one value here that genuinely is a secret:
    # it is what lets this server redeem an authorization code at Google's token
    # endpoint, and it never leaves the server - not to the browser, not to the `web`
    # container (see the `environment:` block in the install bundle's compose file,
    # https://github.com/opendiving/opendiving/blob/main/docker-compose.yml, which names
    # `GOOGLE_CLIENT_ID` and deliberately not this).
    #
    # `SecretStr`, because a value that must not reach a log line is better served by a
    # type whose `repr` cannot spill it into a traceback than by everyone remembering.
    # `Settings` is never dumped anywhere, but "never" is a property to hold structurally.
    #
    # Setting `GOOGLE_CLIENT_ID` without this fails startup - see
    # `Settings._require_google_client_secret`.
    GOOGLE_CLIENT_SECRET: SecretStr | None = config("GOOGLE_CLIENT_SECRET", default=None, cast=SecretStr)


class RegistrationMode(Enum):
    """Who may create an account on this instance.

    `open` is what this app did before the setting existed: any address that proves it owns
    itself gets an account. `invite` admits only an address someone already invited, or
    whoever follows a member's invite link - plus the very first account on an empty
    instance, which is the operator's own and is admitted in *both* modes (see
    `services.registration_gate`).

    Declared as an `Enum` for the reason `EnvironmentOption` is: pydantic rejects a value
    that is not one of these at `Settings()`, which is import time, so a typo names itself
    at startup rather than silently selecting a mode nobody chose.
    """

    OPEN = "open"
    INVITE = "invite"


class AccountSource(StrEnum):
    """The fixed words an account's creation is counted under in the daily totals.

    Every other key of `accounts_created` is a join-channel slug, which is why
    `JOIN_CHANNELS` may not name a slug that is one of these: the two share one key space,
    and a channel called `waitlist` would be counted as the waiting list. Declared here
    rather than beside the counter because the setting is validated against it at import.
    """

    BOOTSTRAP = "bootstrap"
    OPEN = "open"
    INVITATION = "invitation"
    WAITLIST = "waitlist"


# What a join-channel slug may be. The request bodies and the setting's own validator both
# accept exactly this, so a value that cannot be configured cannot be sent; the resolve
# route takes any segment and answers one outside it with the same 404 as an unknown slug.
# A member's invite-link token rides the same `via` field, and is generated to fit it.
JOIN_CHANNEL_SLUG_PATTERN = r"^[a-z0-9-]{1,32}$"
JOIN_CHANNEL_LABEL_MAX_LENGTH = 40

# Identifies this project, and nobody in it, to every third party it calls. The two provider
# settings default to it; an outbound call with no setting of its own sends it as it is.
PROJECT_USER_AGENT = "OpenDiving (+https://github.com/opendiving/opendiving-api)"


def parse_join_channels(raw: str | None) -> dict[str, str]:
    """`JOIN_CHANNELS` as a slug-to-label mapping, refusing a value that is not one.

    Each comma-separated entry is `slug=Label`. A bad entry raises naming it, so a typo stops
    the process at startup rather than quietly making a link dead.
    """
    channels: dict[str, str] = {}
    for pair in split_csv(raw):
        slug, _, label = pair.partition("=")
        slug, label = slug.strip(), label.strip()
        if not re.fullmatch(JOIN_CHANNEL_SLUG_PATTERN, slug):
            raise ValueError(f"JOIN_CHANNELS entry {pair!r}: a slug is 1 to 32 lowercase letters, digits or hyphens.")
        if slug in set(AccountSource):
            raise ValueError(
                f"JOIN_CHANNELS entry {pair!r}: {slug!r} is reserved - the daily totals count "
                f"accounts under {', '.join(source.value for source in AccountSource)} already."
            )
        if slug in channels:
            raise ValueError(f"JOIN_CHANNELS entry {pair!r}: the slug {slug!r} is listed twice.")
        if not label or len(label) > JOIN_CHANNEL_LABEL_MAX_LENGTH:
            raise ValueError(
                f"JOIN_CHANNELS entry {pair!r}: each slug needs a label after '=', "
                f"of at most {JOIN_CHANNEL_LABEL_MAX_LENGTH} characters."
            )
        channels[slug] = label
    return channels


class RegistrationSettings(BaseSettings):
    # **`invite` is the default, and that is a deliberate breaking change.** An instance
    # reachable from the internet with no setting touched would otherwise take anyone who
    # finds it, and the apps in this category that hold personal data all ship closed
    # (Mealie's `ALLOW_SIGNUP=false`, Plausible's `DISABLE_REGISTRATION=invite_only`, a
    # Bluesky PDS's `PDS_INVITE_REQUIRED`, Mastodon's `registrations_mode=none`); the
    # developer tools that ship open (Gitea, Vikunja) hold nobody's dive log. The
    # first-account exemption is what keeps closed-by-default from being a first-run trap:
    # a fresh instance has no invitations and no superuser, and the first address to sign
    # in becomes both.
    #
    # Flipping it is a restart, like every other setting here - they are read once at
    # import.
    REGISTRATION_MODE: RegistrationMode = config("REGISTRATION_MODE", default=RegistrationMode.INVITE)

    # **Whether the OpenDiving project itself operates this instance.** Named for the fact
    # it asserts rather than for a nickname: an operator who sets it is claiming to *be*
    # the project, and the default is what every self-hosted install gets without touching
    # anything. What it does today is select copy. The web app reads it off `GET /config`
    # before the landing page paints and picks the request-an-invite form's wording on it -
    # the generic wording, true of any instance, by default; the project's own waitlist
    # wording ("join the waitlist", "we'll notify you when your spot is ready") when true.
    #
    # This API branches on it in exactly one place, and that branch now exists: the opening
    # sentence of the invitation email (`services.email_service.send_invitation_email`). "X
    # invited you to their log book" is true of a self-hosted instance and wrong on the one
    # the project runs, where the invitee is invited to OpenDiving itself. Keep every use to
    # copy selection and route each one through this field, so a single grep for it lists
    # every place the app knows who runs it.
    PROJECT_OPERATED: bool = config("PROJECT_OPERATED", default=False)

    # **Whether this instance is the one where OpenDiving Commons is edited.** Off, a copy: the
    # Commons rows follow the bundle the image ships. On, the table is the truth, the bundle
    # only seeds it, and `GET /commons/dive-sites` serves it. Off is what a missing variable
    # gives, so nothing an operator forgets can publish anything. `PROJECT_OPERATED` is not this
    # switch: that one selects copy, and this one reaches rows and routes.
    COMMONS_PRIMARY: bool = config("COMMONS_PRIMARY", default=False)

    # What one address may ask of `GET /commons/dive-sites`, the whole pool per request. The
    # publish fetches it daily, so the bound is on a scraper rather than on any caller meant to
    # be here.
    COMMONS_EXPORT_RATE_LIMIT_WINDOW_SECONDS: int = config("COMMONS_EXPORT_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    COMMONS_EXPORT_RATE_LIMIT_PER_IP: int = config("COMMONS_EXPORT_RATE_LIMIT_PER_IP", default=20)

    # How many proposals one account may send a day, each a person's time in the queue. Far past
    # a diver working through their logbook; `POST /dive-site/{uuid}/proposals` answers 429 beyond.
    COMMONS_PROPOSAL_RATE_LIMIT_WINDOW_SECONDS: int = config(
        "COMMONS_PROPOSAL_RATE_LIMIT_WINDOW_SECONDS", default=86400
    )
    COMMONS_PROPOSAL_RATE_LIMIT_PER_USER: int = config("COMMONS_PROPOSAL_RATE_LIMIT_PER_USER", default=30)

    # What one account may ask of `GET /dive-sites/search`, which a search box asks once per
    # debounced keystroke and a map once per *Search this area*. A bound on a burst rather than a
    # budget: a request a second, sustained for five minutes, is past anyone typing.
    DIVE_SITE_SEARCH_RATE_LIMIT_WINDOW_SECONDS: int = config("DIVE_SITE_SEARCH_RATE_LIMIT_WINDOW_SECONDS", default=300)
    DIVE_SITE_SEARCH_RATE_LIMIT_PER_USER: int = config("DIVE_SITE_SEARCH_RATE_LIMIT_PER_USER", default=300)
    # How long that search waits on the geocoder for its places, counted from when it asks. Past
    # it the answer goes without places, so a slow geocoder costs the places and nothing else.
    DIVE_SITE_SEARCH_GEOCODER_BUDGET_SECONDS: float = config("DIVE_SITE_SEARCH_GEOCODER_BUDGET_SECONDS", default=2.0)

    # `slug=Label` pairs, comma-separated: each one a link, `/join?via=<slug>`, that admits
    # whoever follows it without an invitation and counts the account under its slug. Empty
    # is the default and turns the feature off everywhere - the resolve route 404s, `GET
    # /config` says `join_links: false`, and a sign-in carrying a slug is a stale link.
    # Read through `join_channels` below at the moment of each decision and never copied
    # into a module constant, so a channel removed by a restart between a sign-in request
    # and its completion is refused at the completion.
    JOIN_CHANNELS: str | None = config("JOIN_CHANNELS", default=None)

    @field_validator("JOIN_CHANNELS")
    @classmethod
    def _parseable_join_channels(cls, value: str | None) -> str | None:
        parse_join_channels(value)
        return value

    @property
    def join_channels(self) -> dict[str, str]:
        return parse_join_channels(self.JOIN_CHANNELS)

    # A *rate*, not a lifetime allotment: a member who invites five friends today can
    # invite five more tomorrow. Counted from the `invitation` table rather than from the
    # Redis limiter, because the limiter fails open on a Redis outage by design (see
    # `DECISIONS.md` §"Rate limiting fails open on a Redis *outage*") and its counters are
    # flushed locally - neither of which a growth bound on who gets into a closed beta
    # should inherit. Superusers are exempt.
    INVITATIONS_PER_USER: int = config("INVITATIONS_PER_USER", default=5)
    INVITATIONS_WINDOW_DAYS: int = config("INVITATIONS_WINDOW_DAYS", default=1)

    # **A throttle, and not the quota above** - they bound different things and only one of
    # them bounds the oracle. `POST /user/invitations` answers a distinguishable 409 for an
    # address that already has an account, and that refusal creates no invitation row, so
    # the quota (counted from rows created) never charges for it: unthrottled, a signed-in
    # caller can walk a wordlist through the endpoint and learn who is registered, without
    # limit. This is the shape `PATCH /user`'s username check already guards - "unthrottled
    # it is a wordlist oracle over who exists" - and it is keyed per-user for the same
    # reason: the caller is authenticated, so there is a better key than their IP.
    #
    # Deliberately above `INVITATIONS_PER_USER` rather than equal to it. It is a backstop
    # against automated probing, not a second opinion on how many friends somebody may
    # invite, and a diver who mistypes an address twice must not be spending the same
    # budget that decides whether their fifth invitation goes out.
    INVITATION_ATTEMPT_RATE_LIMIT_PER_USER: int = config("INVITATION_ATTEMPT_RATE_LIMIT_PER_USER", default=20)

    # Fixed-window rate limits on `POST /invite-requests`, keyed separately by the
    # submitted email and by client IP - the `ContactSettings` shape, with the support
    # form's values, because it is the same kind of endpoint: anonymous, writing a row
    # nobody authenticated, on behalf of somebody who may never come back.
    INVITE_REQUEST_RATE_LIMIT_WINDOW_SECONDS: int = config("INVITE_REQUEST_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    INVITE_REQUEST_RATE_LIMIT_PER_EMAIL: int = config("INVITE_REQUEST_RATE_LIMIT_PER_EMAIL", default=3)
    INVITE_REQUEST_RATE_LIMIT_PER_IP: int = config("INVITE_REQUEST_RATE_LIMIT_PER_IP", default=10)


class MagicLinkSettings(BaseSettings):
    MAGIC_LINK_TOKEN_EXPIRE_MINUTES: int = config("MAGIC_LINK_TOKEN_EXPIRE_MINUTES", default=30)

    # Fixed-window rate limits (see `core.utils.rate_limit`), keyed separately by email
    # and by client IP - the former stops one address from being spammed, the latter
    # stops one caller from spamming many addresses (and, being far higher, is not a
    # meaningful enumeration side-channel).
    MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS: int = config("MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS", default=900)
    MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL: int = config("MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL", default=3)
    MAGIC_LINK_REQUEST_RATE_LIMIT_PER_IP: int = config("MAGIC_LINK_REQUEST_RATE_LIMIT_PER_IP", default=15)
    # Every endpoint that redeems a proof of an address, each under its own key: the three
    # sign-in verify routes, the email-change link check and verify, and `/auth/google`,
    # whose redeemed code is that proof for a Google account.
    MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP: int = config("MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP", default=30)

    # Guesses allowed against the six-digit code printed beside the link - the right one is
    # charged too - after which the code is spent and only the link still works (see
    # `crud.crud_authentication_requests.charge_code_attempt`).
    #
    # This is the boundary, not the rate limit: 5 guesses in a space of 10^6 is roughly a
    # one-in-200,000 shot per request, and every request emails the victim, so an attack
    # worth running is one they are watching arrive. Raising it is not a knob to reach for
    # casually - each extra guess raises those odds linearly, and the reason the
    # cap can be this tight is that a diver who mistypes twice can simply read the code
    # again from the email that is already open in front of them.
    SIGN_IN_CODE_ATTEMPTS_MAX: int = config("SIGN_IN_CODE_ATTEMPTS_MAX", default=5)

    # Per-IP over the same window.
    #
    # `/auth/complete` is the username-availability oracle (it answers "Username not
    # available"), so it gets a low ceiling - nobody legitimately creates ten accounts
    # from one address in a quarter of an hour. `/auth/restore` spends the same setting
    # under its own key: like `/auth/complete`, it turns the short-lived token a verify
    # step handed back into a session.
    #
    # `/auth/refresh` is deliberately far more generous: it's called on a timer by every
    # open tab, and an office or campus behind one NAT gateway is a single IP as far as
    # this counter is concerned. The limit is here to bound replay of a stolen cookie,
    # not to pace normal use.
    AUTH_COMPLETE_RATE_LIMIT_PER_IP: int = config("AUTH_COMPLETE_RATE_LIMIT_PER_IP", default=10)
    AUTH_REFRESH_RATE_LIMIT_PER_IP: int = config("AUTH_REFRESH_RATE_LIMIT_PER_IP", default=240)

    # Email-change confirmation (see `POST /user/email-change/request`/
    # `POST /user/email-change/verify` in `api.v1.users`) reuses the same
    # `AuthenticationRequest` mechanics as sign-in, with its own expiry/rate limit
    # (keyed per-user, not per-email - it's an authenticated action). The request's per-IP
    # ceiling is five times this rather than a setting of its own: it only stops one
    # address cycling through many accounts.
    EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES: int = config("EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES", default=30)
    EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER: int = config("EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER", default=3)

    # `PATCH /user` answering "Username not available" is the same availability oracle
    # `/auth/complete` is, and a signed-in caller can walk a wordlist through it without
    # even needing a fresh onboarding token. Keyed per-user rather than per-IP because
    # it's authenticated, and applied only when a username change is actually requested -
    # the rest of the profile (name, avatar, email-preference toggle) reveals nothing and
    # shouldn't 429 a settings page.
    USERNAME_CHANGE_RATE_LIMIT_PER_USER: int = config("USERNAME_CHANGE_RATE_LIMIT_PER_USER", default=5)
    # Linking a person to an account by username (`POST`/`PATCH /person`) answers whether
    # that username exists, the oracle above; an import's links count here too. Per user,
    # over the same window, and spent only by a link that names a different account.
    PERSON_LINK_RATE_LIMIT_PER_USER: int = config("PERSON_LINK_RATE_LIMIT_PER_USER", default=10)


class SMTPTLSMode(Enum):
    STARTTLS = "starttls"
    TLS = "tls"
    NONE = "none"


class EmailSettings(BaseSettings):
    # SMTP is the only transport (see `services.email_service`). Every provider speaks it -
    # Resend included, as `SMTP_HOST=smtp.resend.com` with the API key as `SMTP_PASSWORD` -
    # and so does every relay a self-hoster already has. Unset `SMTP_HOST` is the documented
    # local setup, and currently the only one in use: nothing is sent and the magic-link URL
    # is logged instead.
    SMTP_HOST: str | None = config("SMTP_HOST", default=None)
    SMTP_PORT: int = config("SMTP_PORT", default=587)
    # Independently optional, because an anonymous relay is a legitimate setup (Mailpit, an
    # internal postfix). The login is attempted only when a username is configured.
    SMTP_USERNAME: str | None = config("SMTP_USERNAME", default=None)
    SMTP_PASSWORD: SecretStr | None = config("SMTP_PASSWORD", default=None, cast=SecretStr)
    # `starttls` (587) and `tls` (465) both verify certificates. `none` exists for a relay
    # on the loopback or the compose network - Mailpit, a local postfix - and nothing else.
    SMTP_TLS_MODE: SMTPTLSMode = config("SMTP_TLS_MODE", default=SMTPTLSMode.STARTTLS)

    # No default: the old one (`onboarding@resend.dev`) was only ever deliverable on
    # Resend's sandbox domain, and through an arbitrary relay it is an SPF/DKIM failure at
    # send time - hours after the misconfiguration, in someone else's spam folder. The
    # validator on `Settings` turns that into a startup error instead.
    EMAIL_FROM_ADDRESS: str | None = config("EMAIL_FROM_ADDRESS", default=None)


class ContactSettings(BaseSettings):
    # Inbox the frontend's support form (`POST /api/v1/support`) delivers to. The
    # `CONTACT_FORM_` names stay: running instances already set `CONTACT_FORM_EMAIL`, and a
    # rename would be a manual step for every operator.
    #
    # No default, for the same reason `EMAIL_FROM_ADDRESS` has none. A default of this
    # project's own address would quietly route a self-hosted instance's support mail -
    # sign-in trouble, lost dives, whatever they typed - to an inbox belonging to
    # strangers who can neither see that server nor help them.
    # There is no address we can guess that is right for somebody else's install, so unset
    # means the endpoint answers 503 (`api.v1.support`) rather than delivering somewhere.
    CONTACT_FORM_EMAIL: str | None = config("CONTACT_FORM_EMAIL", default=None)

    # Fixed-window rate limits (see `core.utils.rate_limit`), keyed separately by the
    # submitted email and by client IP, mirroring the magic-link limits above. The
    # endpoint is unauthenticated and sends mail, so this is the only thing standing
    # between it and being used as a relay; the window is deliberately much longer
    # than the auth one, since nobody legitimately files ten support requests an hour.
    CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS: int = config("CONTACT_FORM_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    CONTACT_FORM_RATE_LIMIT_PER_EMAIL: int = config("CONTACT_FORM_RATE_LIMIT_PER_EMAIL", default=3)
    CONTACT_FORM_RATE_LIMIT_PER_IP: int = config("CONTACT_FORM_RATE_LIMIT_PER_IP", default=10)


class GeocodingSettings(BaseSettings):
    # Geocoding for dive sites and trips, proxied server-side (see
    # `services.geocoding_service`), over two providers with two wire formats. `GEOCODER_URL`
    # names the spot behind a pin and has to name a Nominatim-compatible host;
    # `GEOCODER_SEARCH_URL` answers a place typed into a search and has to name a
    # Photon-compatible one. Both defaults are keyless public instances, so a self-hoster gets
    # a working feature with no third-party account.
    #
    # `GEOCODER_URL=""` turns geocoding off entirely, search included: disabled means
    # disabled, and an upgrade must not switch it back on. `GEOCODER_SEARCH_URL=""` turns off
    # search alone. Either way the endpoints answer "no result" rather than failing, exactly
    # as they do when a provider is unreachable. Neither default follows the other: a
    # `GEOCODER_URL` naming your own Nominatim still sends searches to public Photon until
    # `GEOCODER_SEARCH_URL` names a host of yours.
    GEOCODER_URL: str = config("GEOCODER_URL", default="https://nominatim.openstreetmap.org")
    GEOCODER_SEARCH_URL: str = config("GEOCODER_SEARCH_URL", default="https://photon.komoot.io")
    # Sent to `GEOCODER_URL` only; Photon takes no key.
    GEOCODER_API_KEY: SecretStr | None = config("GEOCODER_API_KEY", default=None, cast=SecretStr)

    # Asked for explicitly, because the alternative is not "no preference" - it is the
    # *local* script. Left unset, a reverse lookup of the Blue Hole answers
    # "دهب, جنوب سيناء, مصر", which then lands in a dive site locality's `name` and is neither
    # readable nor typeable for most of the divers who log that site. One instance-wide value
    # rather than the caller's `Accept-Language`: it is part of the cache key, and per-caller
    # languages would multiply both the cache and the outbound calls by the number of locales.
    # Photon's public instance answers only `de`, `en` and `fr`, so a search asks in English
    # for any other value.
    GEOCODER_LANGUAGE: str = config("GEOCODER_LANGUAGE", default="en")

    # Nominatim's policy requires a `User-Agent` that identifies the application, and
    # blocks generic ones; Photon's maintainers give a generic one no guarantees. Sent to
    # both. A public deployment that isn't this project's own should say so here, since the
    # address in it is where the providers' operators will complain.
    GEOCODER_USER_AGENT: str = config("GEOCODER_USER_AGENT", default=PROJECT_USER_AGENT)

    # What one account may spend, and the only limit on these endpoints that can produce a
    # 429 - the provider cap below degrades instead. So this is purely an abuse bound, not
    # a pacing mechanism, and it is sized for the worst *legitimate* pattern rather than the
    # typical one: `/geocode/search` backs a type-ahead, which fires once per debounced
    # keystroke, and a diver adding sites for a week's trip can produce hundreds of calls in
    # an evening without doing anything unreasonable. It counts cache hits too, which
    # overstates the real cost - another reason to leave it loose.
    GEOCODER_RATE_LIMIT_WINDOW_SECONDS: int = config("GEOCODER_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    GEOCODER_RATE_LIMIT_PER_USER: int = config("GEOCODER_RATE_LIMIT_PER_USER", default=600)

    # What the *instance* may spend on each provider, counted across all users and applied
    # only to calls that actually leave (a cache hit costs nothing). One limit, two counters:
    # a pin's lookup and a search keystroke never wait on each other. The default is
    # Nominatim's published cap of one request per second; Photon publishes none, and one a
    # second is well inside the "reasonable limit" its public instance asks for. Raising it
    # raises both caps, so a self-hoster running their own Nominatim should raise it only
    # when `GEOCODER_SEARCH_URL` also names a host they run.
    #
    # Exceeding it is *not* a 429: the counter is global, so raising would mean one diver's
    # search rejecting another's. The call is skipped and logged, and the caller gets the
    # same "no suggestion" these endpoints already answer with when the provider is down.
    GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS: int = config("GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS", default=1)
    GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS: int = config("GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS", default=1)


class SeafloorSettings(BaseSettings):
    # The seafloor explorer's depth grids (`services.seafloor`), fetched server-side from public
    # bathymetry services. Every source is keyless but one, so the feature works with nothing
    # set.
    #
    # The data.go.kr service key for the Korea Hydrographic and Oceanographic Agency's water
    # depth API, the finest source in Korean waters. Unset, that source is reported as not
    # configured and Korean waters are answered by the global ones. data.go.kr issues it both
    # encoded and decoded, and either works. Its quota is 10,000 requests a day, and a view the
    # cache cannot answer spends one or two.
    KHOA_API_KEY: SecretStr | None = config("KHOA_API_KEY", default=None, cast=SecretStr)
    # What one account may spend on `GET /seafloor`, and the only limit on it, which 429s. Cache
    # hits count too. A miss can spend a dozen third-party requests and some of the KHOA quota
    # above, which every account shares, hence the dive-conditions preview's figure rather than
    # the geocoder's.
    SEAFLOOR_RATE_LIMIT_PER_USER: int = config("SEAFLOOR_RATE_LIMIT_PER_USER", default=120)
    SEAFLOOR_RATE_LIMIT_WINDOW_SECONDS: int = config("SEAFLOOR_RATE_LIMIT_WINDOW_SECONDS", default=3600)


def normalize_map_renderer_url(raw: str) -> str:
    """`MAP_RENDERER_URL` as an origin `services.map_renderer` can call, or `""` for off.

    A bare `host:port` is taken as plain HTTP, because that is the form a Render Blueprint's
    `fromService … property: hostport` supplies for a private service. `urlparse` would read
    the host of `renderer:10000` as a scheme, so the bare form is recognised by its missing
    `://` before anything parses it.
    """
    value = raw.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    try:
        # `port` raises on one that is no number or out of range, which `urlparse` lets
        # through and httpx would refuse only at the first call, as an `InvalidURL`.
        names_renderer = parsed.scheme in ("http", "https") and bool(parsed.hostname) and parsed.port != 0
    except ValueError:
        names_renderer = False
    if not names_renderer:
        raise ValueError(
            f"MAP_RENDERER_URL is {raw!r}, which names no renderer. Set it in your .env to the "
            "renderer's host:port or http(s) URL, or leave it empty to draw no map tiles."
        )
    return value.rstrip("/")


class MapRendererSettings(BaseSettings):
    # The map renderer that draws the map tiles every dive, trip and dive site card and page head
    # is composed from (`services.map_renderer`), a service of its own reachable only inside the
    # stack. Empty, the default, draws none: `GET /config` says `map_tiles: false` and the tile
    # and style routes answer 404.
    MAP_RENDERER_URL: str = config("MAP_RENDERER_URL", default="")

    # Seconds one draw may take, queueing at the renderer included: a margin over the renderer's
    # full queue at its slowest cold tile. Raise it, never lower it, where the queue times your
    # renderer's slowest cold draw exceeds it, and keep any proxy in front of the API waiting at
    # least this long.
    MAP_RENDERER_TIMEOUT: float = config("MAP_RENDERER_TIMEOUT", default=90.0)

    # Two limits per account over one window, each answered 429 past it. Draws an account may
    # start: a first view of ten cards and a page head can ask for six tiles each, so the default
    # admits that twice over; a stored tile, and a request waiting on another's draw of the same
    # one, are never counted. Tile requests, stored ones and waits included: tiles are shared, and
    # a stored one answers faster than a drawn one, so this is the rate at which an account can
    # probe what this instance has drawn. Style requests count here too.
    MAP_RENDERER_DRAW_LIMIT_PER_USER: int = config("MAP_RENDERER_DRAW_LIMIT_PER_USER", default=150)
    MAP_RENDERER_REQUEST_LIMIT_PER_USER: int = config("MAP_RENDERER_REQUEST_LIMIT_PER_USER", default=1200)
    MAP_RENDERER_LIMIT_WINDOW_SECONDS: int = config("MAP_RENDERER_LIMIT_WINDOW_SECONDS", default=60)

    @field_validator("MAP_RENDERER_URL")
    @classmethod
    def _callable_map_renderer_url(cls, value: str) -> str:
        return normalize_map_renderer_url(value)

    @field_validator("MAP_RENDERER_TIMEOUT")
    @classmethod
    def _positive_map_renderer_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError(f"MAP_RENDERER_TIMEOUT is {value}; a draw needs a deadline above zero seconds.")
        return value

    @property
    def map_tiles(self) -> bool:
        return bool(self.MAP_RENDERER_URL)


class SpeciesSettings(BaseSettings):
    # The species catalog's upstream sources, proxied server-side exactly as geocoding is
    # (see `services.species_service`). None takes a key. The two name registers below are
    # not optional in the sense `GEOCODER_URL` is: emptying them does not switch the feature
    # off, it degrades search to whatever is already in the local catalog and makes resolving
    # a *new* species fail. That asymmetry is deliberate - a diver can type a location by
    # hand, but they cannot invent an AphiaID. `COMMONS_API_URL` is the one that genuinely is
    # optional: unset or unreachable, a species simply gets no photo.
    #
    # WoRMS is the taxonomic authority: scientific names, synonyms, and the accepted-taxon
    # mapping that gives every catalog row its identity. Its REST webservice is free to use
    # with citation and asks only that it not be used to harvest the register wholesale,
    # which is why this app looks things up one pick at a time instead of importing.
    WORMS_API_URL: str = config("WORMS_API_URL", default="https://www.marinespecies.org/rest")
    # Wikidata is the common-name layer, keyed to WoRMS by property P850, and CC0. It is here
    # because WoRMS alone cannot answer "clownfish": *Amphiprion ocellaris* carries exactly
    # one vernacular in WoRMS, and it is in Japanese.
    WIKIDATA_API_URL: str = config("WIKIDATA_API_URL", default="https://www.wikidata.org/w/api.php")
    # Wikimedia Commons, asked for a photo's credit metadata and thumbnail URL once a Wikidata
    # entity has named the file (P18) or an admin has, and - for an admin choosing one - for
    # the files in a species' Commons category. Unlike the two above it is asked nothing a
    # diver typed - it receives file and category titles derived from an AphiaID.
    #
    # **A setting, while the hosts the bytes are fetched from are hard-coded.** That asymmetry
    # is the point: `services.species_photos` will only ever fetch image bytes from the two
    # hosts a Commons `imageinfo` reply names - `thumb.wikimedia.org` for a scaled thumbnail and
    # `upload.wikimedia.org` for an original, which is what a file narrower than the width asked
    # for comes back as - which is an SSRF
    # fence, and a fence with an environment variable in front of it is not a fence. This
    # setting exists so an instance can point the metadata call at a mirror - and so the
    # "unroutable host degrades to no photo" behaviour is checkable without editing code.
    COMMONS_API_URL: str = config("COMMONS_API_URL", default="https://commons.wikimedia.org/w/api.php")

    # Wikimedia's policy requires a descriptive `User-Agent` and blocks generic ones; WoRMS
    # asks to be told who is calling. One string satisfies both. A public deployment that
    # isn't this project's own should say so here - it is the address either provider's
    # operators will use before they block you.
    SPECIES_USER_AGENT: str = config("SPECIES_USER_AGENT", default=PROJECT_USER_AGENT)

    # What one account may spend across `/species/search`, `/species/resolve` and the
    # `/user/species/suggest` calls that reach the search, together, and the only limit here
    # that can produce a 429. Sized like the geocoder's for the same
    # reason: search backs a type-ahead, so an evening of logging a week's dives can honestly
    # produce hundreds of calls, and it counts cache hits too.
    SPECIES_RATE_LIMIT_WINDOW_SECONDS: int = config("SPECIES_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    SPECIES_RATE_LIMIT_PER_USER: int = config("SPECIES_RATE_LIMIT_PER_USER", default=600)

    # What the *instance* may spend on each provider, counted across all users and charged
    # only to calls that actually leave. Exceeding one is not a 429 - the counter is global,
    # so raising would mean one diver's search rejecting another's. That provider simply
    # contributes nothing to the search, and the other one still answers.
    #
    # Neither number is published by the provider it throttles. WoRMS states no rate limit at
    # all, and Wikimedia's applies to anonymous heavy use rather than to a call every few
    # seconds. Both are self-imposed politeness, set well above what a picker generates, and
    # a self-hoster with a relationship with either can raise them.
    SPECIES_WORMS_RATE_LIMIT_WINDOW_SECONDS: int = config("SPECIES_WORMS_RATE_LIMIT_WINDOW_SECONDS", default=60)
    SPECIES_WORMS_RATE_LIMIT_REQUESTS: int = config("SPECIES_WORMS_RATE_LIMIT_REQUESTS", default=120)
    SPECIES_WIKIDATA_RATE_LIMIT_WINDOW_SECONDS: int = config("SPECIES_WIKIDATA_RATE_LIMIT_WINDOW_SECONDS", default=60)
    SPECIES_WIKIDATA_RATE_LIMIT_REQUESTS: int = config("SPECIES_WIKIDATA_RATE_LIMIT_REQUESTS", default=300)
    # Commons gets a pair of its own because `_claim_provider_slot` indexes its `limits` dict
    # unguarded - a provider with no entry there is a `KeyError`, not a missing throttle.
    # Lower than Wikidata's: this one is charged at most twice per *new* species (the credit
    # call and the byte fetch) rather than once per enriched search candidate, so a number
    # that would bottleneck a picker cannot bottleneck this. An admin adds to it - two or three
    # metadata calls to open the photo picker, two for a pin - but the picker's previews are
    # charged nothing: a dialog's worth would spend a large share of the window in one click
    # and leave a diver's resolve beside it with no slot. The picker's own cap and one admin's
    # pace bound those instead.
    SPECIES_COMMONS_RATE_LIMIT_WINDOW_SECONDS: int = config("SPECIES_COMMONS_RATE_LIMIT_WINDOW_SECONDS", default=60)
    SPECIES_COMMONS_RATE_LIMIT_REQUESTS: int = config("SPECIES_COMMONS_RATE_LIMIT_REQUESTS", default=120)


DIVE_CONDITIONS_PROVIDERS = ("open-meteo",)


class DiveConditionsSettings(BaseSettings):
    # The provider the worker asks for a dive's surface weather, sea state and tide
    # (`services.dive_conditions`). Empty, the default, asks nobody: the cron returns before
    # it reads a row, so no install calls a third party whose terms its operator has not
    # read. Open-Meteo's free API is for non-commercial use only.
    DIVE_CONDITIONS_PROVIDER: str = config("DIVE_CONDITIONS_PROVIDER", default="")
    # A subscriber's key, which moves every call to Open-Meteo's `customer-` hosts. It has to
    # be on a plan that includes the Historical Weather API: a key without it is refused on
    # the archive and nothing is filled.
    DIVE_CONDITIONS_OPEN_METEO_API_KEY: SecretStr | None = config(
        "DIVE_CONDITIONS_OPEN_METEO_API_KEY", default=None, cast=SecretStr
    )
    # What the whole instance may spend on Open-Meteo, charged only to calls that leave. The
    # default sits under the free API's hourly, daily and monthly caps together - 9,600 a day
    # against 10,000, 288,000 a month against 300,000 - which leaves room for one instance
    # behind an address, not two. Over it, the run stops and the rest wait for a later one.
    DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS: int = config(
        "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", default=400
    )
    DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_WINDOW_SECONDS: int = config(
        "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_WINDOW_SECONDS", default=3600
    )
    # What one account may spend on `GET /dive-conditions/lookup`, the dive form's preview, and
    # the only limit on it that can 429. Cache hits count too. A fifth of the geocoder's: a
    # lookup the cache cannot answer spends two of the instance's calls above, which the worker
    # shares, so one diver's evening of logging must leave the worker room.
    DIVE_CONDITIONS_LOOKUP_RATE_LIMIT_PER_USER: int = config("DIVE_CONDITIONS_LOOKUP_RATE_LIMIT_PER_USER", default=120)
    DIVE_CONDITIONS_LOOKUP_RATE_LIMIT_WINDOW_SECONDS: int = config(
        "DIVE_CONDITIONS_LOOKUP_RATE_LIMIT_WINDOW_SECONDS", default=3600
    )

    @field_validator("DIVE_CONDITIONS_PROVIDER")
    @classmethod
    def _known_dive_conditions_provider(cls, value: str) -> str:
        value = value.strip()
        if value and value not in DIVE_CONDITIONS_PROVIDERS:
            raise ValueError(
                f"DIVE_CONDITIONS_PROVIDER is {value!r}. Set it in your .env to one of "
                f"{', '.join(DIVE_CONDITIONS_PROVIDERS)}, or leave it empty to fetch no conditions."
            )
        return value

    @property
    def dive_conditions(self) -> bool:
        return bool(self.DIVE_CONDITIONS_PROVIDER)


class ExportSettings(BaseSettings):
    # Fixed-window rate limit (see `core.utils.rate_limit`) on the four `/export/*`
    # endpoints, keyed per user and shared between them - the budget bounds total export
    # work, so the cheap CSV download draws on the same allowance as the archive.
    #
    # Authenticated and owner-only, so this is not an abuse
    # boundary the way the support form's is - it is there because one archive request
    # reads every file the caller stored, and nothing else in the API does that. The bound
    # is deliberately generous: this is a button a diver presses once, and someone
    # scripting a nightly backup of their own account should not hit it.
    EXPORT_RATE_LIMIT_WINDOW_SECONDS: int = config("EXPORT_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    EXPORT_RATE_LIMIT_PER_USER: int = config("EXPORT_RATE_LIMIT_PER_USER", default=10)


class LogbookImportSettings(BaseSettings):
    # Its own budget rather than a share of the export one: an import is a different kind
    # of expensive - it spools up to half a gigabyte, parses a whole logbook and may make
    # outbound WoRMS calls - and a diver restoring a backup should not find their next
    # export refused because of it. An import is one upload, and ten an hour is the same
    # generosity the export limit means by ten downloads. Its apply counts apart, on the same
    # figures.
    IMPORT_RATE_LIMIT_WINDOW_SECONDS: int = config("IMPORT_RATE_LIMIT_WINDOW_SECONDS", default=3600)
    IMPORT_RATE_LIMIT_PER_USER: int = config("IMPORT_RATE_LIMIT_PER_USER", default=10)

    # How long an uploaded import waits to be applied or discarded before it and its files are
    # deleted. Blank keeps it until the diver finishes it; below 1 refuses startup
    # (`Settings._require_usable_import_lifetimes`). A change applies to imports uploaded from
    # then on: the row holds its own expiry.
    IMPORT_PENDING_LIFETIME_HOURS: int | None = config("IMPORT_PENDING_LIFETIME_HOURS", default=24)

    # How long an applied import's review and log stay in its history. Blank keeps each until
    # the diver deletes it; below 1 refuses startup. A change applies to imports applied from
    # then on.
    IMPORT_HISTORY_RETENTION_DAYS: int | None = config("IMPORT_HISTORY_RETENTION_DAYS", default=30)

    @field_validator("IMPORT_PENDING_LIFETIME_HOURS", "IMPORT_HISTORY_RETENTION_DAYS", mode="before")
    @classmethod
    def _blank_import_lifetime_is_none(cls, value: object) -> object:
        """A blank value is no limit, arriving from `env_file` as the empty string."""
        return None if isinstance(value, str) and not value.strip() else value

    # Total wall-clock budget for the WoRMS pre-pass that resolves species the catalog does
    # not hold yet. Sized against `resolve_species`'s own ceiling rather than its typical
    # cost: one unknown AphiaID can spend two `_RESOLVE_BUDGET_SECONDS` passes (the synonym
    # branch) plus `_ENRICHMENT_BUDGET_SECONDS`, about a minute, so this is two of those.
    # Unknowns still unresolved when it runs out are skipped and reported, never guessed -
    # a species link is the one thing an import may drop without losing a dive.
    IMPORT_SPECIES_BUDGET_SECONDS: float = config("IMPORT_SPECIES_BUDGET_SECONDS", default=120.0)


class FileStorageBackendOption(Enum):
    """Where `services/blob_store.py` keeps uploaded payloads.

    Declared as an `Enum` for the reason `EnvironmentOption` and `RegistrationMode` are:
    pydantic rejects a value that is not one of these at `Settings()`, which is import
    time, so `FILE_STORAGE_BACKEND=S3` or `=minio` names itself at startup rather than
    silently falling back to the local volume and writing a hosted instance's uploads into
    a container layer.
    """

    LOCAL = "local"
    S3 = "s3"


class FileStorageSettings(BaseSettings):
    # Which of the two stores `services/blob_store.py` writes to. `local` is the default
    # and stays it: every compose install mounts a named volume at `FILE_STORAGE_DIR`, and
    # a self-hoster on one machine gains nothing from an object store but a credential pair
    # to manage. The default is also what keeps the validator below off everyone's back -
    # it cannot fire on a configuration nobody edited.
    FILE_STORAGE_BACKEND: FileStorageBackendOption = config(
        "FILE_STORAGE_BACKEND", default=FileStorageBackendOption.LOCAL
    )

    # Where uploaded dive-computer exports and c-card images are stored on the `local`
    # backend. Everything under it is written and read by `services/blob_store.py` and by
    # nothing else.
    #
    # No new required `.env` value, on purpose: both compose files mount a named volume at
    # this default, so a correct install needs nothing typed. It is a setting at all
    # because a bind-mount install wants to point it at a real directory - and because the
    # test suite and CI have to repoint it away from a path they cannot create.
    FILE_STORAGE_DIR: str = config("FILE_STORAGE_DIR", default="/data/files")

    # The `s3` backend's group. Ignored entirely while the backend is `local`, and all four
    # of the required ones are refused-if-missing by `Settings._require_s3_credentials` the
    # moment it is not - a half-configured object store must not start and then 500 on the
    # first upload.
    #
    # The full endpoint origin, including the scheme: R2's
    # `https://<account>.eu.r2.cloudflarestorage.com`, MinIO's `http://minio:9000`. There
    # is no default, because every store's is different and a guess would be a wrong one.
    S3_ENDPOINT_URL: str | None = config("S3_ENDPOINT_URL", default=None)
    S3_BUCKET: str | None = config("S3_BUCKET", default=None)
    S3_ACCESS_KEY_ID: str | None = config("S3_ACCESS_KEY_ID", default=None)

    # `SecretStr` for the reason `GOOGLE_CLIENT_SECRET` above is one: a value that must not
    # reach a log line is better served by a type whose `repr` cannot spill it into a
    # traceback than by everyone remembering.
    S3_SECRET_ACCESS_KEY: SecretStr | None = config("S3_SECRET_ACCESS_KEY", default=None, cast=SecretStr)

    # `auto` is what Cloudflare R2 documents and what every other S3-compatible store
    # ignores, so it is the default that needs no thought. Point this at a real AWS bucket
    # and it has to name that bucket's region instead.
    S3_REGION: str = config("S3_REGION", default="auto")

    # An optional key prefix, for sharing one bucket between instances. Prepended to every
    # object name and stripped back off by `blob_store.iter_keys`, so the keys the database
    # holds are identical on both backends and an install can move either way.
    S3_PREFIX: str | None = config("S3_PREFIX", default=None)

    # How much one account may keep stored, in MB (1024-based, as every size here is): its
    # dive-computer files as stored - compressed - plus its card scans and pictures. An
    # upload that would take the account past it is refused with a 413; nothing already
    # stored is ever removed. On by default, because the operator pays for every byte and
    # "free for everyone" without one means one account can fill the disk. Blank turns it
    # off; below 1 refuses startup (`Settings._require_a_usable_storage_limit`).
    STORAGE_LIMIT_MB: int | None = config("STORAGE_LIMIT_MB", default=1024)

    @field_validator("STORAGE_LIMIT_MB", mode="before")
    @classmethod
    def _blank_storage_limit_is_none(cls, value: object) -> object:
        """A blank value is no limit. Compose hands `.env` to the process through
        `env_file`, where `STORAGE_LIMIT_MB=` arrives as the empty string, which an integer
        field would refuse at startup."""
        return None if isinstance(value, str) and not value.strip() else value


#: The four `S3_*` settings that have no sensible default and no safe absence.
#: `S3_REGION` and `S3_PREFIX` are not here: both have defaults that work.
S3_REQUIRED_SETTINGS: tuple[str, ...] = (
    "S3_ENDPOINT_URL",
    "S3_BUCKET",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
)


def missing_s3_settings(values: FileStorageSettings) -> list[str]:
    """Which of `S3_REQUIRED_SETTINGS` are unset or blank, in declaration order.

    Shared by the startup validator below and by `blob_store.backend_for`, which can be
    asked for an S3 backend while `FILE_STORAGE_BACKEND` is still `local` - that is exactly
    what `src/scripts/migrate_blobs.py` does, and it has to fail with the same message
    rather than a `NoneType` deep inside botocore.
    """
    missing: list[str] = []
    for name in S3_REQUIRED_SETTINGS:
        value = getattr(values, name, None)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if not (value or "").strip():
            missing.append(name)
    return missing


class ProxySettings(BaseSettings):
    # Addresses (or CIDR blocks) of reverse proxies whose `X-Forwarded-For` header may be
    # believed - see `core.utils.client_ip`. Every per-IP rate limit depends on this:
    # unset, a deployment behind nginx sees one client (the proxy) and throttles everyone
    # into a single shared bucket. Set wrongly - i.e. trusting a network that isn't
    # actually in front of you - callers can forge the header and evade the limits.
    #
    # Comma-separated, e.g. "172.16.0.0/12" for a Docker bridge network, or the address
    # of the load balancer. Leave unset when the app is reached directly.
    #
    # A plain string, split in `core.utils.client_ip`, rather than a `list[str]`: for a
    # complex field type pydantic-settings parses the environment variable itself and
    # expects JSON, so `TRUSTED_PROXY_IPS=172.16.0.0/12` fails validation at startup no
    # matter what `cast=` does here (that only produces the default). `split_csv` at the
    # top of this file parses all three settings that took this trade - this one and the
    # two `CRUD_ADMIN_ALLOWED_*` below.
    TRUSTED_PROXY_IPS: str | None = config("TRUSTED_PROXY_IPS", default=None)


class FrontendSettings(BaseSettings):
    # Used to build the magic-link URL emailed to the user (`{FRONTEND_URL}/auth/verify?token=...`),
    # and - through `passkey_rp_id`/`passkey_origin` below - it *is* the passkey domain.
    #
    # Changing its hostname orphans every registered passkey, because browsers scope a
    # credential to the RP ID it was created under and will not offer it to another. The
    # magic link is the recovery path when that happens. `src/.env.example` carries the same
    # warning where an operator sets this, and the operator-facing version is under "Sign-in"
    # in https://github.com/opendiving/opendiving/blob/main/docs/configuration.md
    FRONTEND_URL: str = config("FRONTEND_URL", default="http://localhost:3000")

    @property
    def passkey_rp_id(self) -> str:
        """The WebAuthn Relying Party ID: the bare hostname of `FRONTEND_URL`.

        Derived rather than configured, so there is no second place for it to be wrong -
        the same reasoning that rejected a `PASSKEYS_ENABLED` knob. The
        ceremony belongs to the *frontend* origin; this API's own host never appears in it,
        which the split-origin dev topology makes impossible to get accidentally right.

        `localhost` in dev, which browsers treat as a secure context, so the whole feature
        works locally over plain HTTP. An IP address is *not* a valid RP ID no matter what
        certificate fronts it - the browser offers the API and then throws `SecurityError`.
        """
        return urlparse(self.FRONTEND_URL).hostname or ""

    @property
    def frontend_origin(self) -> str:
        """The frontend's origin, as a browser writes it in `Origin` and `clientDataJSON`.

        **Rebuilt from the parse, never the raw setting.** Browsers write a bare
        `scheme://host[:port]`, so a `FRONTEND_URL` with a trailing slash - the most
        ordinary way to write a URL variable - would match no request's origin: CORS would
        refuse every split-origin call and every passkey ceremony would 401, while magic
        links (plain concatenation) kept working, pointing nowhere near the cause.
        """
        parsed = urlparse(self.FRONTEND_URL)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def passkey_origin(self) -> str:
        """The origin a `clientDataJSON` from that frontend will carry."""
        return self.frontend_origin

    @property
    def google_redirect_uri(self) -> str:
        """Where Google sends the visitor back after they approve the sign-in, and the
        `redirect_uri` this server will exchange an authorization code against.

        Rebuilt from the parse rather than concatenated, for the reason `frontend_origin`
        above gives: a `FRONTEND_URL` with a trailing slash is the most ordinary way to
        write a URL variable, and `{FRONTEND_URL}/auth/google/callback` would then produce
        a double slash that Google matches against nothing it has registered.

        Derived rather than configured, so there is no second place for it to be wrong -
        but the browser still *sends* the URI it used, and `POST /auth/google` refuses to
        exchange against any other. That comparison is a diagnostic rather than a control:
        Google already binds a code to the URI it saw and will not accept one it has no
        registration for, so what the check buys is that a `FRONTEND_URL` disagreeing with
        the origin the visitor actually reached fails in this app's own error, naming this
        setting, instead of as a `redirect_uri_mismatch` from Google that names neither.
        """
        return f"{self.frontend_origin}/auth/google/callback"


class PasskeySettings(BaseSettings):
    """Everything passkeys need beyond `FRONTEND_URL`, all defaulted - registering one
    adds nothing to any install's required configuration.
    """

    # How long a minted challenge stays in Redis (`auth:passkey-challenge:*`). Long enough
    # to sit on the login page with conditional UI armed, short enough to bound how long a
    # captured ceremony has to be replayed - and the challenge is `GETDEL`-consumed on the
    # first verify attempt regardless, so this is only the ceiling on an *unused* one.
    PASSKEY_CHALLENGE_TTL_SECONDS: int = config("PASSKEY_CHALLENGE_TTL_SECONDS", default=600)

    # At most this many credentials per account. Abuse hygiene rather than product policy:
    # a diver with a phone, a laptop and two security keys is nowhere near it.
    PASSKEY_MAX_CREDENTIALS_PER_USER: int = config("PASSKEY_MAX_CREDENTIALS_PER_USER", default=10)

    # Fixed-window rate limits over `MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS`, the window every
    # auth limit shares.
    #
    # The options ceiling is high on purpose and matches `AUTH_REFRESH_RATE_LIMIT_PER_IP`
    # exactly: conditional UI arms on every signed-out page view that supports it, including
    # the landing-page hero, so options are minted at page-view frequency, and an office
    # behind one NAT gateway is a single IP to this counter. `/auth/refresh` faced the same
    # situation while firing *less* often, so anything lower here contradicts that call. If
    # abuse ever shows, the lever is arming conditional UI on first focus of the email input
    # rather than on mount - not a lower ceiling.
    #
    # None of these is the security boundary. The single-use challenge and the signature
    # are; rate limiting fails open (see `core.utils.rate_limit`).
    PASSKEY_OPTIONS_RATE_LIMIT_PER_IP: int = config("PASSKEY_OPTIONS_RATE_LIMIT_PER_IP", default=240)
    PASSKEY_VERIFY_RATE_LIMIT_PER_IP: int = config("PASSKEY_VERIFY_RATE_LIMIT_PER_IP", default=30)
    PASSKEY_REGISTER_RATE_LIMIT_PER_USER: int = config("PASSKEY_REGISTER_RATE_LIMIT_PER_USER", default=10)


class ApiKeySettings(BaseSettings):
    # At most this many API keys per account, expired ones included. Abuse hygiene rather
    # than product policy, as the passkey cap is: a diver with a sync script and a laptop
    # is nowhere near it.
    API_KEY_MAX_PER_USER: int = config("API_KEY_MAX_PER_USER", default=10)

    # Creations per account over `MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS`, the window every
    # auth limit shares. Each one sends an email, so this bounds a create-and-revoke loop
    # as a mailer, as `PASSKEY_REGISTER_RATE_LIMIT_PER_USER` does.
    API_KEY_CREATE_RATE_LIMIT_PER_USER: int = config("API_KEY_CREATE_RATE_LIMIT_PER_USER", default=10)


class GearServiceSettings(BaseSettings):
    # Hour of the day (UTC) the three scheduled emails run - the gear-service digest, the
    # renewal reminders and the year in review (`core/worker/settings.py`). Configurable
    # mainly so local development can park it somewhere harmless; 07:00 UTC lands
    # mid-morning across Europe, which is close enough given everything here is
    # date-granular.
    GEAR_SERVICE_DIGEST_HOUR: int = config("GEAR_SERVICE_DIGEST_HOUR", default=7)


class AccountDeletionSettings(BaseSettings):
    # How long a deletion request sits reversible before `purge_deleted_accounts`
    # destroys the account (see `core.worker.functions`). The account is dark from the
    # moment the button is pressed either way - this is only how long the way back in
    # stays open, not a period the app keeps working.
    #
    # `0` means the next hourly sweep purges it, which is the setting an operator who
    # wants no grace period at all would reach for. It changes the confirmation email's
    # copy rather than being a special case in the job: at zero there is nothing to tell
    # the user to sign in before.
    #
    # 14 rather than 30, and the reason is a published sentence: the bundled privacy page
    # promises erasure "within 30 days", and a 30-day window swept hourly lands at "30
    # days and change". Raising this past roughly 29 makes that sentence false for the
    # instance - https://github.com/opendiving/opendiving/blob/main/docs/configuration.md
    # says so.
    ACCOUNT_DELETION_GRACE_DAYS: int = config("ACCOUNT_DELETION_GRACE_DAYS", default=14)

    # Fixed-window per-user limit over `MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS`, because
    # `DELETE /user` sends mail. Deliberately not the thing that stops a double submit -
    # a two-request race beats any counter, which is why the endpoint's real guard is a
    # database predicate (`WHERE is_deleted = false`) and this is only here to stop one
    # account being used to pump the relay.
    ACCOUNT_DELETION_RATE_LIMIT_PER_USER: int = config("ACCOUNT_DELETION_RATE_LIMIT_PER_USER", default=5)


# One password for both Redis pools. The bundled `redis` container needs none and the
# default is unset, but a managed Redis - or any server with `requirepass` - always wants
# one, and the cache and the arq queue are the same server in every configuration this app
# ships. Two variables would be two ways to get one thing half-right.
_REDIS_PASSWORD: SecretStr | None = config("REDIS_PASSWORD", default=None, cast=SecretStr)


def redis_url(host: str, port: int, password: str | None) -> str:
    """A `redis://` DSN, percent-encoding the password for the same reason
    `postgres_uri` encodes its credentials.
    """
    credentials = f":{quote(password, safe='')}@" if password else ""
    return f"redis://{credentials}{host}:{port}"


class RedisSettings(BaseSettings):
    REDIS_PASSWORD: SecretStr | None = _REDIS_PASSWORD


class RedisCacheSettings(RedisSettings):
    REDIS_CACHE_HOST: str = config("REDIS_CACHE_HOST", default="localhost")
    REDIS_CACHE_PORT: int = config("REDIS_CACHE_PORT", default=6379)

    @property
    def REDIS_CACHE_URL(self) -> SecretStr:
        """Built from the parts, as `POSTGRES_URI` is."""
        password = self.REDIS_PASSWORD.get_secret_value() if self.REDIS_PASSWORD else None
        return SecretStr(redis_url(self.REDIS_CACHE_HOST, self.REDIS_CACHE_PORT, password))


class ClientSideCacheSettings(BaseSettings):
    CLIENT_CACHE_MAX_AGE: int = config("CLIENT_CACHE_MAX_AGE", default=60)


class RedisQueueSettings(RedisSettings):
    REDIS_QUEUE_HOST: str = config("REDIS_QUEUE_HOST", default="localhost")
    REDIS_QUEUE_PORT: int = config("REDIS_QUEUE_PORT", default=6379)


class CRUDAdminSettings(BaseSettings):
    # Off by default. The panel is mounted at a fixed, guessable path and bypasses the
    # whole `api.v1` authorization story (it talks to the models directly), so it has
    # to be something an operator turns on deliberately rather than something a fresh
    # deploy inherits. `src/.env.example` ships the whole block commented out, local
    # development included: turning the panel on is an edit, never an inheritance.
    CRUD_ADMIN_ENABLED: bool = config("CRUD_ADMIN_ENABLED", default=False)
    CRUD_ADMIN_MOUNT_PATH: str = config("CRUD_ADMIN_MOUNT_PATH", default="/admin")

    # Where the panel keeps its *own* tables (admin users, sessions, event log) - not the
    # application's data, which it reads through the normal `async_get_db`.
    #
    # Unset means CRUDAdmin's default: a SQLite file under `crudadmin_data/`, local to
    # whichever container's filesystem happens to create it. That is fine for one process
    # and wrong for anything else - two workers race to create the tables and to insert
    # the initial admin, and a one-shot init container would write a file the API
    # container can never see. Point this at Postgres and all of that goes away.
    # A `SecretStr`: pointed at Postgres, it is a DSN carrying the password.
    CRUD_ADMIN_DB_URL: SecretStr | None = config("CRUD_ADMIN_DB_URL", default=None, cast=SecretStr)

    # Who may reach the panel at all, comma-separated and parsed by `split_csv`, e.g.
    # CRUD_ADMIN_ALLOWED_IPS="203.0.113.7,203.0.113.8" or
    # CRUD_ADMIN_ALLOWED_NETWORKS="10.0.0.0/8". Unset means "from anywhere the API is
    # reachable", which is what `_reject_insecure_admin_config` warns about outside `local`.
    #
    # These were `list[str] | None` bare annotations with no `config()` call, for the
    # pydantic-settings reason `split_csv` documents - which made them settable only as
    # JSON, and only through an *exported* environment variable rather than the `src/.env`
    # file every other setting here comes from and the docs teach. Comma-strings make them
    # configurable the same way as `TRUSTED_PROXY_IPS`, which had already made this trade.
    CRUD_ADMIN_ALLOWED_IPS: str | None = config("CRUD_ADMIN_ALLOWED_IPS", default=None)
    CRUD_ADMIN_ALLOWED_NETWORKS: str | None = config("CRUD_ADMIN_ALLOWED_NETWORKS", default=None)
    CRUD_ADMIN_MAX_SESSIONS: int = config("CRUD_ADMIN_MAX_SESSIONS", default=10)
    CRUD_ADMIN_SESSION_TIMEOUT: int = config("CRUD_ADMIN_SESSION_TIMEOUT", default=1440)
    SESSION_SECURE_COOKIES: bool = config("SESSION_SECURE_COOKIES", default=True)

    CRUD_ADMIN_TRACK_EVENTS: bool = config("CRUD_ADMIN_TRACK_EVENTS", default=True)
    CRUD_ADMIN_TRACK_SESSIONS: bool = config("CRUD_ADMIN_TRACK_SESSIONS", default=True)

    CRUD_ADMIN_REDIS_ENABLED: bool = config("CRUD_ADMIN_REDIS_ENABLED", default=False)
    CRUD_ADMIN_REDIS_HOST: str = config("CRUD_ADMIN_REDIS_HOST", default="localhost")
    CRUD_ADMIN_REDIS_PORT: int = config("CRUD_ADMIN_REDIS_PORT", default=6379)
    CRUD_ADMIN_REDIS_DB: int = config("CRUD_ADMIN_REDIS_DB", default=0)
    # `default=None`, like every other optional setting here, rather than the *string*
    # "None" - a sentinel that silently becomes a real password the moment anyone writes
    # CRUD_ADMIN_REDIS_PASSWORD="None" meaning it literally.
    CRUD_ADMIN_REDIS_PASSWORD: SecretStr | None = config("CRUD_ADMIN_REDIS_PASSWORD", default=None, cast=SecretStr)


class EnvironmentOption(Enum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class EnvironmentSettings(BaseSettings):
    ENVIRONMENT: EnvironmentOption = config("ENVIRONMENT", default=EnvironmentOption.LOCAL)


LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class LoggingSettings(BaseSettings):
    # Any level name the stdlib knows (DEBUG, INFO, WARNING, ERROR, CRITICAL), case
    # insensitive. Applied by `configure_logging` below; validated at startup rather than
    # left for `logging.basicConfig` to raise on, so a typo names itself.
    LOG_LEVEL: str = config("LOG_LEVEL", default="INFO")


def configure_logging(level: str) -> None:
    """Point the root logger at stderr at `level`.

    Called by both entrypoints - `core.setup` for the API, `core.worker.functions` for the
    worker - because there is no third place both of them already import. This module
    owns `LOG_LEVEL`, so it owns applying it.

    stderr rather than stdout because that is `basicConfig`'s default stream and there is
    no reason to fight it: both are captured identically by `docker compose logs` and by
    every collector, and `uvicorn` and `arq` already log there.

    The level is set on the root logger explicitly as well as passed to `basicConfig`,
    which does nothing at all when the root logger already has a handler - under a server
    that installed its own, `LOG_LEVEL` would otherwise be exactly the dead configuration
    described above.
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    logging.getLogger().setLevel(level)


class Settings(
    AppSettings,
    PostgresSettings,
    CryptSettings,
    FirstUserSettings,
    GoogleAuthSettings,
    RegistrationSettings,
    MagicLinkSettings,
    EmailSettings,
    ContactSettings,
    GeocodingSettings,
    SeafloorSettings,
    MapRendererSettings,
    SpeciesSettings,
    DiveConditionsSettings,
    ExportSettings,
    LogbookImportSettings,
    FileStorageSettings,
    ProxySettings,
    FrontendSettings,
    PasskeySettings,
    ApiKeySettings,
    GearServiceSettings,
    AccountDeletionSettings,
    RedisCacheSettings,
    ClientSideCacheSettings,
    RedisQueueSettings,
    CRUDAdminSettings,
    EnvironmentSettings,
    LoggingSettings,
):
    # A validator's refusal otherwise prints `input_value=` - the whole environment dict,
    # truncated, which can end in the tail of a password.
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    @model_validator(mode="after")
    def _reject_placeholder_secret_key(self) -> Self:
        """Refuses to boot on a `SECRET_KEY` that is published somewhere.

        `SECRET_KEY` signs every access, refresh and onboarding token this app issues, so
        a value anyone can read is a key anyone can mint tokens with - full impersonation
        of any account, including a superuser's. The canonical setup is
        `cp src/.env.example src/.env`, and that template has to carry *something* in the
        slot, so the value it carries must not be one an instance can run with.

        Every environment, with no `ENVIRONMENT` gate: a staging instance signing tokens
        with a published key is compromised in exactly the way a production one is, and
        the local instance is the one whose `.env` came straight from the template. The one
        exception is `CI_SECRET_KEY`, refused everywhere but `local`, which is how CI boots.

        The message names `.env` and not `src/.env.example`. This refusal has a *second*
        audience the template path does not describe: the front door's `example.env` ships
        `change-me-openssl-rand-hex-32`, the same string this module lists as a placeholder,
        and its by-hand install downloads that file straight to `.env` and tells the operator
        to replace six values in it. Someone who starts the stack a step early meets this
        error with no `src/` tree anywhere on the machine, so naming the template would send
        them to a file they cannot open. `.env` is the file both of them are editing. (The
        `install.sh` path generates the key before writing `.env`, so it never reaches here -
        which is why the by-hand install is the case that decides the wording rather than an
        edge of it.)

        **The first seven words are load-bearing and are not ours to reword.** The front
        door's `docs/configuration`-neighbouring `docs/troubleshooting.md` opens an entry
        with `SECRET_KEY is unset or still a placeholder` in bold, as the string an operator
        pastes in from their terminal; nothing over there is generated from this file, so a
        rewrite of that prefix silently stops the page being findable by the error it is
        about. `test_the_refusal_keeps_the_prefix_the_front_door_indexes_on` pins it. Change
        it only together with that repository, and only as a deliberate pair.
        """
        secret = self.SECRET_KEY.get_secret_value().strip().lower()
        published = secret in PLACEHOLDER_SECRET_KEYS or (
            secret == CI_SECRET_KEY and self.ENVIRONMENT != EnvironmentOption.LOCAL
        )

        if not secret or published:
            raise ValueError(
                "SECRET_KEY is unset or still a placeholder - the one your .env arrived with. It "
                "signs every token this app issues, so a published value lets anyone mint one for "
                "any account. Generate your own: openssl rand -hex 32"
            )
        return self

    @model_validator(mode="after")
    def _require_smtp_outside_local(self) -> Self:
        """Refuses to boot a deployed instance that cannot send mail.

        Sign-in is passwordless: without a relay there is no way to deliver a magic link,
        so *nobody* can get in - not even the first user, on a freshly installed instance.
        The failure is otherwise invisible until someone tries, and then it is a 500 from
        `POST /auth/email/request` (`services.email_service` refuses to log a live token
        off `local`, deliberately) with a message about email transport on a page about
        signing in.

        Every environment except `local`. `local` is the one where the logged-link
        fallback *is* the documented sign-in flow - the developer reads the link out of
        `docker compose logs api` - and anything else is a deployment someone other than
        the developer can reach. Not `production` alone: that staging is close enough to
        local to be run the same way holds only while staging is a second laptop. A staging
        box more than one person can reach is a real deployment, and the sign-in links in its
        logs are real credentials.
        """
        if self.ENVIRONMENT != EnvironmentOption.LOCAL and not self.SMTP_HOST:
            raise ValueError(
                f"ENVIRONMENT is {self.ENVIRONMENT.value} but SMTP_HOST is not set. Sign-in is "
                "passwordless, so without a mail relay nobody can sign in at all - including the "
                "first user. Point SMTP_* at any relay you trust, or run this instance as "
                "ENVIRONMENT=local."
            )
        return self

    @model_validator(mode="after")
    def _require_google_client_secret(self) -> Self:
        """Refuses to boot an instance that offers Google sign-in but cannot complete one.

        The browser never receives a token from Google: it comes back with an
        authorization code, and this server redeems that code at Google's token endpoint -
        which takes both halves of the OAuth client. So a `GOOGLE_CLIENT_ID` with no secret
        is an instance that renders the button and then 401s every click.

        Refusing rather than warning and treating Google as unconfigured, because the
        warning cannot be made to work end to end. The web app decides whether to render
        the button from **its own** `GOOGLE_CLIENT_ID` and has no way to learn that the API
        lacks a secret, so degrading here would leave a visible button that always fails.
        Failing startup puts the error where the mistake was made, which is the same
        argument `_require_from_address_with_smtp` above makes.

        What this guarantees is exactly one thing: *an API that is running has both halves
        of its Google credentials.* It does not promise that sign-in works - a `FRONTEND_URL`
        disagreeing with the origin the visitor reached still breaks it (`POST /auth/google`
        says so, naming the setting), and a redirect URI not registered in the Google Cloud
        Console still breaks it at Google. Three failures, three distinct messages, none of
        them silent.

        Only this direction. A secret with no client id is an instance with Google sign-in
        switched off and one stray variable, which is harmless and not worth a startup
        failure.

        This lives on the combined class rather than on `GoogleAuthSettings`, where both
        fields are declared, because a cross-field validator has to see the whole settings
        object - the same reason the four validators around it are here.
        """
        if not self.GOOGLE_CLIENT_ID:
            return self

        secret = self.GOOGLE_CLIENT_SECRET.get_secret_value().strip() if self.GOOGLE_CLIENT_SECRET else ""
        if not secret:
            raise ValueError(
                "GOOGLE_CLIENT_ID is set but GOOGLE_CLIENT_SECRET is not. Signing in with Google "
                "now redeems an authorization code at Google's token endpoint, which needs both "
                "halves of the OAuth client. Copy the client secret from the same Google Cloud "
                "Console credential the id came from, or unset GOOGLE_CLIENT_ID to turn Google "
                "sign-in off."
            )
        return self

    @model_validator(mode="after")
    def _require_a_usable_storage_limit(self) -> Self:
        """Refuses to boot with a storage limit of nothing.

        `STORAGE_LIMIT_MB=0` reads as "no limit" to anyone who has met that convention
        elsewhere and would mean the opposite here - every upload refused, on an instance
        that otherwise looks healthy. A limit below 1 MB is never what an operator meant, so
        it fails startup naming the setting, and blank is the one spelling of "no limit".
        """
        if self.STORAGE_LIMIT_MB is not None and self.STORAGE_LIMIT_MB < 1:
            raise ValueError(
                f"STORAGE_LIMIT_MB is {self.STORAGE_LIMIT_MB}, and a limit below 1 MB would refuse every "
                "upload. Set it in your .env to the MB one account may store, or leave it blank for no "
                "limit."
            )
        return self

    @model_validator(mode="after")
    def _require_usable_import_lifetimes(self) -> Self:
        """Refuses to boot with an import lifetime below 1.

        `0` is refused rather than read: beside `ACCOUNT_DELETION_GRACE_DAYS`, where it means
        "on the next sweep", it reads as that to one operator and as "no limit" to another, and
        blank is the one spelling of no limit.
        """
        for name, value, unit in (
            ("IMPORT_PENDING_LIFETIME_HOURS", self.IMPORT_PENDING_LIFETIME_HOURS, "hours"),
            ("IMPORT_HISTORY_RETENTION_DAYS", self.IMPORT_HISTORY_RETENTION_DAYS, "days"),
        ):
            if value is not None and value < 1:
                raise ValueError(
                    f"{name} is {value}. Set it in your .env to the {unit} an import is kept, at least 1, or "
                    "leave it blank for no limit."
                )
        return self

    @model_validator(mode="after")
    def _require_s3_credentials(self) -> Self:
        """Refuses to boot an instance told to keep uploads in a bucket it cannot name.

        The failure this replaces is the quiet one: with a backend of `s3` and no endpoint,
        the first upload reaches botocore, which builds a request against `None` and raises
        something about a parameter validation deep in a library the operator has never
        heard of - on a diver's request, hours after the deploy, with the c-card they just
        photographed lost. Naming the missing variable at startup puts the error where the
        mistake was made, which is the argument every validator around this one makes.

        Scoped to the `s3` backend, and that is what keeps it harmless. `local` is the
        default, so nothing that has not opted in can trip it - including `src/.env.example`
        copied verbatim, which is the configuration this whole class is most easily broken
        against: a cross-field validator that fires on the template's own values takes out
        `docker compose up` and pytest collection together.

        The message names `.env` and not `src/.env.example`. A self-hoster installs the
        bundle from the front door - `docker-compose.yml`, `Caddyfile`, `install.sh` and
        `example.env` - and has no `src/` tree to open. What
        they do have is `.env`, written out of `example.env` one line at a time precisely so
        the comments around each setting survive into it, object-storage block included; a
        developer's `src/.env` is the same file by the same route. One name is true for both.
        """
        if self.FILE_STORAGE_BACKEND is not FileStorageBackendOption.S3:
            return self

        missing = missing_s3_settings(self)
        if missing:
            raise ValueError(
                f"FILE_STORAGE_BACKEND is s3 but {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set. Uploaded dive-computer exports "
                "and c-card images have nowhere to go without them. Set the S3_* group - the "
                "object-storage block in your .env documents each one - or set "
                "FILE_STORAGE_BACKEND=local to keep using a filesystem volume."
            )
        return self

    @model_validator(mode="after")
    def _normalize_log_level(self) -> Self:
        """Rejects a `LOG_LEVEL` the stdlib doesn't know, rather than letting
        `configure_logging` raise from inside application startup where the traceback
        points at logging rather than at the typo.
        """
        level = self.LOG_LEVEL.strip().upper()

        if level not in logging.getLevelNamesMapping():
            known = ", ".join(sorted(logging.getLevelNamesMapping()))
            raise ValueError(f"LOG_LEVEL={self.LOG_LEVEL!r} is not a logging level. Use one of: {known}.")

        self.LOG_LEVEL = level
        return self

    @model_validator(mode="after")
    def _require_from_address_with_smtp(self) -> Self:
        """Refuses to boot an instance that has a relay configured but no address to send
        as.

        `EMAIL_FROM_ADDRESS` has no default: a provider's sandbox address is deliverable on
        that provider alone, and through an arbitrary relay it is an SPF/DKIM failure that
        surfaces hours later, in a recipient's spam folder, with nothing in this app's logs
        pointing at the cause. Failing startup puts the error where the mistake was made.

        This is why `src/.env.example` ships the from-address *commented out*: the
        canonical setup is copying that file, so a template with a plausible-looking
        address active in it would satisfy any check made here and reintroduce exactly
        the failure this exists to prevent.
        """
        if self.SMTP_HOST and not self.EMAIL_FROM_ADDRESS:
            raise ValueError(
                "SMTP_HOST is set but EMAIL_FROM_ADDRESS is not. Set it to an address on a "
                "domain your relay is allowed to send for - there is no safe default, and a "
                "wrong one fails at delivery time rather than here."
            )
        return self

    @model_validator(mode="after")
    def _reject_insecure_admin_config(self) -> Self:
        """Refuses to boot a deployed instance whose admin panel is reachable with a
        credential that isn't one.

        `SECRET_KEY` already fails startup when unset; the admin password deserves the
        same treatment, because the panel it guards is a full CRUD interface over
        `User`, `Dive`, `GearItem` and everything else (`admin.views`). Getting this
        wrong is silent - the app starts, serves traffic, and simply happens to have an
        open door - so the check has to happen here rather than being left to a reader
        of `.env.example`. Both templates ship a password commented out, so uncommenting
        one as written must fail here rather than become a working public login.

        Every environment except `local`, for `_require_smtp_outside_local`'s reason: a
        staging instance is a deployment someone other than the developer can reach.
        """
        if not self.CRUD_ADMIN_ENABLED or self.ENVIRONMENT == EnvironmentOption.LOCAL:
            return self

        password = self.ADMIN_PASSWORD.get_secret_value().strip() if self.ADMIN_PASSWORD else ""
        if not password or password.lower() in PLACEHOLDER_ADMIN_PASSWORDS:
            # The front door's troubleshooting page files this under its production wording,
            # `CRUD_ADMIN_ENABLED is true in production but ADMIN_PASSWORD is unset`, so the
            # prefix keeps that shape: change it only together with that page.
            raise ValueError(
                f"CRUD_ADMIN_ENABLED is true in {self.ENVIRONMENT.value} but ADMIN_PASSWORD is unset "
                "or still a placeholder. Set a real ADMIN_PASSWORD, or set CRUD_ADMIN_ENABLED=false "
                "to not expose the admin panel at all."
            )

        if not split_csv(self.CRUD_ADMIN_ALLOWED_IPS) and not split_csv(self.CRUD_ADMIN_ALLOWED_NETWORKS):
            # Advisory rather than fatal: plenty of deployments put the panel behind a
            # VPN or bastion instead, and hard-failing would break those.
            warnings.warn(
                f"The admin panel is enabled with ENVIRONMENT={self.ENVIRONMENT.value} and no "
                "CRUD_ADMIN_ALLOWED_IPS or CRUD_ADMIN_ALLOWED_NETWORKS. It is reachable from "
                "anywhere that can reach the API.",
                stacklevel=2,
            )

        return self


settings = Settings()
