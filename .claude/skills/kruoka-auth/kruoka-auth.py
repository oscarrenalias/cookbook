#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["playwright>=1.40", "keyring>=25", "httpx>=0.27"]
# ///
"""Capture a K-Ruoka web session into the macOS Keychain.

K-Ruoka's first-party API (/kr-api/...) needs a logged-in browser session plus
Cloudflare clearance. This script opens a real Chrome window, waits while the
human logs in, then harvests everything a later API client needs:

  session         account session cookie (~3 month lifetime)
  cf_clearance    Cloudflare clearance cookie (short-lived, bound to IP + UA)
  __cf_bm         Cloudflare bot-management cookie (short-lived)
  user agent      must be replayed verbatim or cf_clearance stops matching
  build number    X-K-Build-Number header; a stale value yields HTTP 409
  store id        e.g. N131, read from the Plussa profile's favourite store
  store name      cosmetic, so `status` is readable

A logged-out visitor also receives a `session` cookie, and product search
serves them normally, so neither proves anything. Capture waits until the
active basket reports a real account before it will finish.

The password is never seen, typed, or stored by this script. Login happens in
the browser window, by hand, including any MFA or Cloudflare challenge.

Credentials resolve from environment variables first and the Keychain second,
so downstream consumers only ever need the env vars and stay portable to a
headless environment on the same machine.

Subcommands:
  capture      open Chrome, wait for login, harvest, validate, store
  status       report presence and age of each credential (never the values)
  export-env   print a shell export block for handing off to another process

Usage:
  ./kruoka-auth.py capture
  ./kruoka-auth.py status --validate
  ./kruoka-auth.py export-env > ~/.kruoka-env
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import os
import pathlib
import shlex
import sys
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

BASE_URL = "https://www.k-ruoka.fi"
SKILL_DIR = pathlib.Path(__file__).resolve().parent
PROFILE_DIR = SKILL_DIR / ".profile"
LOCK_PATH = SKILL_DIR / ".profile.lock"

# Keychain service name and environment variable per credential field.
FIELD_KEYS = {
    "session": ("kruoka-session", "KRUOKA_SESSION"),
    "cf_clearance": ("kruoka-cf-clearance", "KRUOKA_CF_CLEARANCE"),
    "cf_bm": ("kruoka-cf-bm", "KRUOKA_CF_BM"),
    "user_agent": ("kruoka-user-agent", "KRUOKA_USER_AGENT"),
    "build_number": ("kruoka-build-number", "KRUOKA_BUILD_NUMBER"),
    "store_id": ("kruoka-store-id", "KRUOKA_STORE_ID"),
    "store_name": ("kruoka-store-name", "KRUOKA_STORE_NAME"),
    "captured_at": ("kruoka-captured-at", "KRUOKA_CAPTURED_AT"),
}

# Where to look for a shell-style credentials file, for hosts with no Keychain.
# The repo copy comes first: a sandboxed agent is often confined to its
# workspace and cannot read the home directory, nor set an environment
# variable pointing elsewhere.
ENV_FILE_VAR = "KRUOKA_ENV_FILE"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
ENV_FILE_CANDIDATES = (
    REPO_ROOT / ".kruoka-env",
    pathlib.Path.home() / ".kruoka-env",
)

# Records where load() found each field, so `status` can report it.
LAST_SOURCES: dict[str, str] = {}

# Cloudflare only issues cf_clearance when it actually challenges the browser,
# so a capture without it is normal rather than a failure. The store name is
# cosmetic: it makes `status` readable but nothing depends on it.
OPTIONAL_FIELDS = {"cf_clearance", "cf_bm", "store_name"}

VALIDATION_QUERY = "maito"

# Seconds between "are we logged in yet?" API probes while capture waits.
AUTH_PROBE_SECONDS = 5.0

# Only print login instructions if the run is not finishing on its own by now.
QUIET_SECONDS = 4.0


class AuthError(Exception):
    """An actionable error that is safe to print without credential values."""


@dataclass
class Credentials:
    session: str = ""
    cf_clearance: str = ""
    cf_bm: str = ""
    user_agent: str = ""
    build_number: str = ""
    store_id: str = ""
    store_name: str = ""
    captured_at: str = ""

    def missing_required(self) -> list[str]:
        return [
            f.name
            for f in fields(self)
            if f.name not in OPTIONAL_FIELDS and not getattr(self, f.name).strip()
        ]

    def cookies(self) -> dict[str, str]:
        jar = {"session": self.session}
        if self.cf_clearance:
            jar["cf_clearance"] = self.cf_clearance
        if self.cf_bm:
            jar["__cf_bm"] = self.cf_bm
        return jar

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "X-K-Build-Number": self.build_number,
        }


# --------------------------------------------------------------------------
# Credential storage
# --------------------------------------------------------------------------

def _keyring():
    """Import lazily so `status` still works if the Keychain backend is absent."""
    import keyring

    return keyring


def read_env_file(path: pathlib.Path | None = None) -> dict[str, str]:
    """Parse a shell-style credentials file, as written by `export-env`.

    Accepts both `export KEY=value` and bare `KEY=value`, with shell quoting.
    This exists so a machine without a Keychain -- a headless agent on the
    same network, say -- can pick credentials up from a file without anything
    having to set environment variables for it, and without a restart when
    the short-lived Cloudflare cookies are refreshed.
    """
    if path is None:
        configured = os.environ.get(ENV_FILE_VAR, "").strip()
        if configured:
            path = pathlib.Path(configured)
        else:
            path = next((c for c in ENV_FILE_CANDIDATES if c.is_file()),
                        ENV_FILE_CANDIDATES[0])
    if not path.is_file():
        return {}

    mode = path.stat().st_mode
    if mode & 0o077:
        print(
            f"Warning: {path} is readable by other users; chmod 600 it.",
            file=sys.stderr,
        )

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, raw = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        values[key] = parts[0] if parts else ""
    return values


def load() -> Credentials:
    """Resolve credentials: environment, then a credentials file, then Keychain.

    Downstream consumers only ever need one of these to be present. The
    Keychain is a macOS convenience for interactive use; the file is how a
    headless host gets the same credentials with no Keychain and no config.
    """
    creds = Credentials()
    account = getpass.getuser()
    keyring_module = None
    from_file: dict[str, str] | None = None
    LAST_SOURCES.clear()

    for name, (service, env_var) in FIELD_KEYS.items():
        value = os.environ.get(env_var, "").strip()
        source = "env" if value else ""

        if not value:
            if from_file is None:
                from_file = read_env_file()
            value = from_file.get(env_var, "").strip()
            source = "file" if value else source

        if not value and sys.platform == "darwin":
            if keyring_module is None:
                try:
                    keyring_module = _keyring()
                except ImportError:  # pragma: no cover - dependency is declared
                    keyring_module = False
            if keyring_module:
                with contextlib.suppress(Exception):
                    value = (keyring_module.get_password(service, account) or "").strip()
                    source = "keychain" if value else source

        setattr(creds, name, value)
        LAST_SOURCES[name] = source or "missing"
    return creds


def store(creds: Credentials) -> None:
    """Write credentials to the Keychain via the Keychain Services API.

    Deliberately not `security add-generic-password -w <value>`, which would
    expose each secret in the process table to any same-user process.
    """
    if sys.platform != "darwin":
        raise AuthError(
            "Keychain storage is macOS-only. On another platform, capture on the "
            "Mac and move the credentials across with `export-env`."
        )
    keyring_module = _keyring()
    account = getpass.getuser()
    for name, (service, _) in FIELD_KEYS.items():
        value = getattr(creds, name)
        if value:
            keyring_module.set_password(service, account, value)


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------

@contextlib.contextmanager
def profile_lock():
    """Stop two captures from driving the same Chrome profile directory."""
    LOCK_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuthError(
                "Another capture is already running; finish that browser window first."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def capture(timeout: int, store_hint: str | None) -> Credentials:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - dependency is declared inline
        raise AuthError(
            "Playwright is not available. Run this script via `uv run --script` "
            "(the shebang does this automatically) so its dependencies resolve."
        ) from None

    PROFILE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Anything the site sends to /kr-api/ while the window is open, we keep.
    sniffed: dict[str, str] = {}

    def remember(request) -> None:
        if "/kr-api/" not in request.url:
            return
        build = request.headers.get("x-k-build-number")
        if build and build.isdecimal():
            sniffed["build_number"] = build
        # Search puts storeId in the query string; basket/active puts it in the
        # JSON body. Accepting either means a plain page load is usually enough.
        store_id = parse_qs(urlparse(request.url).query).get("storeId", [""])[0]
        if not store_id:
            with contextlib.suppress(Exception):
                body = request.post_data_json
                if isinstance(body, dict):
                    store_id = body.get("storeId") or ""
        if isinstance(store_id, str) and store_id.strip():
            sniffed["store_id"] = store_id.strip()

    def on_response(response) -> None:
        # Only trust a build number the server actually accepted.
        if response.ok:
            with contextlib.suppress(Exception):
                remember(response.request)

    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                viewport=None,
                # Cloudflare's Turnstile widget fails outright if it spots the
                # automation fingerprint, so strip what we reasonably can.
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"],
            )
        except PlaywrightError as exc:
            raise AuthError(
                f"Could not launch Chrome: {exc}. Make sure Google Chrome is "
                "installed and no other capture is using this profile."
            ) from None

        try:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            context.on("response", on_response)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(BASE_URL, wait_until="domcontentloaded")
            user_agent = page.evaluate("navigator.userAgent")

            start = time.monotonic()
            deadline = start + timeout
            cookies: dict[str, str] = {}
            last_probe = 0.0
            authenticated = False
            prompted = False
            while time.monotonic() < deadline:
                with contextlib.suppress(Exception):
                    cookies = {
                        c["name"]: c["value"] for c in context.cookies(BASE_URL)
                    }
                ready = (
                    cookies.get("session")
                    and "build_number" in sniffed
                    and "store_id" in sniffed
                )
                # An anonymous visitor gets a `session` cookie as well, so cookie
                # presence proves nothing. Ask the API whether a real account is
                # attached, and keep waiting until it says yes.
                if ready and time.monotonic() - last_probe >= AUTH_PROBE_SECONDS:
                    last_probe = time.monotonic()
                    authenticated, store_name = probe_account(
                        _assemble(cookies, sniffed, user_agent)
                    )
                    if authenticated:
                        sniffed["store_name"] = store_name
                        break
                # Stay quiet on the common path, where the persistent profile is
                # still signed in and the whole run takes a few seconds.
                if not prompted and time.monotonic() - start > QUIET_SECONDS:
                    prompted = True
                    print(
                        "Chrome is open. In that window, log in to K-Ruoka.\n"
                        "An anonymous visit is not enough -- the site issues those a\n"
                        "session cookie too, so capture waits for a real account.\n"
                        "Your store comes from your Plussa profile; you need not pick one.\n"
                        "Waiting...",
                        file=sys.stderr,
                    )
                time.sleep(1.0)
            else:
                raise AuthError(_timeout_hint(cookies, sniffed, timeout))
        finally:
            with contextlib.suppress(Exception):
                context.close()

    creds = _assemble(cookies, sniffed, user_agent)
    missing = creds.missing_required()
    if missing:
        raise AuthError(f"Capture incomplete, missing: {', '.join(missing)}.")
    if store_hint and creds.store_id != store_hint:
        raise AuthError(
            f"Captured store {creds.store_id} but expected {store_hint}. "
            "Select the intended store in the browser and capture again."
        )
    return creds


def _assemble(cookies: dict, sniffed: dict, user_agent: str) -> Credentials:
    return Credentials(
        session=cookies.get("session", ""),
        cf_clearance=cookies.get("cf_clearance", ""),
        cf_bm=cookies.get("__cf_bm", ""),
        user_agent=user_agent,
        build_number=sniffed.get("build_number", ""),
        store_id=sniffed.get("store_id", ""),
        store_name=sniffed.get("store_name", ""),
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _timeout_hint(cookies: dict, sniffed: dict, timeout: int) -> str:
    if not cookies.get("session"):
        return (
            f"Timed out after {timeout}s without a session cookie. Run capture "
            "again and open the site in the browser window."
        )
    if "store_id" not in sniffed or "build_number" not in sniffed:
        return (
            f"No /kr-api/ call was seen within {timeout}s, so the store id and "
            "build number could not be read. Run capture again and search for any "
            "product in the browser window before it closes."
        )
    return (
        f"Timed out after {timeout}s: the session is still anonymous. K-Ruoka "
        "issues a session cookie to logged-out visitors too, so capture waits for "
        "a real account. Run capture again and complete the login."
    )


# --------------------------------------------------------------------------
# Live validation
# --------------------------------------------------------------------------

def probe_account(creds: Credentials) -> tuple[bool, str]:
    """Return (signed in?, store display name) by reading the active basket.

    k-ruoka.fi issues a `session` cookie to anonymous visitors as well, and
    product search answers them happily, so neither is evidence of a login.
    The active basket echoes a `userInfo` block that stays entirely blank until
    an account is attached; that is the signal we trust.
    """
    import httpx

    if not creds.session or not creds.build_number or not creds.store_id:
        return False, ""
    try:
        response = httpx.post(
            f"{BASE_URL}/kr-api/basket/active",
            headers=creds.headers(),
            cookies=creds.cookies(),
            json={
                "storeId": creds.store_id,
                "substitutionDefault": True,
                "skipClearClosedDeliverySlot": False,
            },
            timeout=httpx.Timeout(5.0, read=30.0),
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return False, ""
    if not response.is_success:
        return False, ""
    try:
        body = response.json()
    except ValueError:
        return False, ""
    user_info = body.get("userInfo") or {}
    store_name = str((body.get("store") or {}).get("name") or "").strip()
    # Presence only; these values are personal data and are never read out.
    signed_in = any(
        str(user_info.get(field, "")).strip()
        for field in ("email", "firstName", "lastName", "phoneNumber")
    )
    return signed_in, store_name


def is_authenticated(creds: Credentials) -> bool:
    return probe_account(creds)[0]


def validate(creds: Credentials) -> str:
    """Make real API calls to prove the credentials work AND are logged in."""
    import httpx

    missing = creds.missing_required()
    if missing:
        raise AuthError(
            f"Cannot validate, missing: {', '.join(missing)}. Run `capture` first."
        )

    url = f"{BASE_URL}/kr-api/v2/product-search/{VALIDATION_QUERY}"
    params = {
        "offset": 0,
        "language": "fi",
        "storeId": creds.store_id,
        "limit": 1,
        "discountFilter": "false",
        "isTrOffer": "false",
    }
    try:
        response = httpx.post(
            url,
            params=params,
            headers=creds.headers(),
            cookies=creds.cookies(),
            timeout=httpx.Timeout(5.0, read=30.0),
            follow_redirects=False,
        )
    except httpx.TimeoutException:
        raise AuthError("K-Ruoka timed out. Check your connection and retry.") from None
    except httpx.HTTPError:
        raise AuthError("Could not reach K-Ruoka. Check your connection.") from None

    if response.headers.get("cf-mitigated") == "challenge":
        raise AuthError(
            "Cloudflare challenged the request. Run `capture` again and complete "
            "the challenge in the browser window."
        )
    if response.status_code in (401, 403):
        raise AuthError(
            f"K-Ruoka returned HTTP {response.status_code}; the session has expired. "
            "Run `capture` again."
        )
    if response.status_code == 409:
        current = response.headers.get("K-Ruoka-Build", "unknown")
        raise AuthError(
            f"Build number {creds.build_number} is stale (server reports {current}). "
            "Run `capture` again to pick up the current build."
        )
    if not response.is_success:
        raise AuthError(f"K-Ruoka returned HTTP {response.status_code}.")

    total = ""
    with contextlib.suppress(Exception):
        total = f", {response.json().get('totalHits', '?')} hits for '{VALIDATION_QUERY}'"
    if not is_authenticated(creds):
        raise AuthError(
            f"Search works (HTTP {response.status_code}{total}) but the session is "
            "anonymous, so Plussa prices and offers will not be visible. Run "
            "`capture` again and log in."
        )
    return f"HTTP {response.status_code}{total}; signed in"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _age(captured_at: str) -> str:
    try:
        then = datetime.fromisoformat(captured_at)
    except ValueError:
        return "unknown age"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    if hours < 1:
        return f"{hours * 60:.0f} minutes old"
    if hours < 48:
        return f"{hours:.0f} hours old"
    return f"{hours / 24:.0f} days old"


def cmd_capture(args) -> int:
    with profile_lock():
        creds = capture(args.timeout, args.store_hint)
    where = f"{creds.store_id} ({creds.store_name})" if creds.store_name else creds.store_id
    print(f"Captured store {where}, build {creds.build_number}.")
    print(f"Validating... {validate(creds)}")
    store(creds)
    print("Stored in the Keychain. Run `status` any time to re-check.")
    return 0


def cmd_status(args) -> int:
    creds = load()
    for name, (_service, _env_var) in FIELD_KEYS.items():
        value = getattr(creds, name)
        source = LAST_SOURCES.get(name, "?")
        if not value:
            note = "MISSING" + (" (optional)" if name in OPTIONAL_FIELDS else "")
        elif name in ("store_id", "build_number", "store_name"):
            note = f"{value} [{source}]"  # not secret, and useful to see
        elif name == "captured_at":
            note = f"{value} ({_age(value)}) [{source}]"
        else:
            note = f"set, {len(value)} chars [{source}]"
        print(f"{name:14} {note}")

    missing = creds.missing_required()
    if missing:
        print(
            f"\nMissing: {', '.join(missing)}. Run `kruoka-auth.py capture`.",
            file=sys.stderr,
        )
        return 1
    if args.validate:
        print(f"\nLive check: {validate(creds)}")
    return 0


def cmd_export_env(_args) -> int:
    creds = load()
    missing = creds.missing_required()
    if missing:
        raise AuthError(
            f"Nothing to export, missing: {', '.join(missing)}. Run `capture` first."
        )
    print(
        "# Secrets follow. Redirect to a file with mode 0600, never commit them.",
        file=sys.stderr,
    )
    for name, (_, env_var) in FIELD_KEYS.items():
        value = getattr(creds, name)
        if value:
            print(f"export {env_var}={shlex.quote(value)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_capture = sub.add_parser("capture", help="Open Chrome, log in, harvest, validate")
    p_capture.add_argument(
        "--timeout", type=int, default=300, help="Seconds to wait for login (default: 300)"
    )
    p_capture.add_argument(
        "--store-hint", help="Fail unless this store id is the one selected, e.g. N190"
    )
    p_capture.set_defaults(func=cmd_capture)

    p_status = sub.add_parser("status", help="Report credential presence and age")
    p_status.add_argument(
        "--validate", action="store_true", help="Also make one live API call"
    )
    p_status.set_defaults(func=cmd_status)

    p_export = sub.add_parser("export-env", help="Print a shell export block")
    p_export.set_defaults(func=cmd_export_env)

    args = parser.parse_args(argv)
    if getattr(args, "timeout", 1) < 1:
        parser.error("--timeout must be positive")
    try:
        return args.func(args)
    except AuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
