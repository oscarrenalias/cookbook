---
name: kruoka-auth
description: Capture a logged-in K-Ruoka web session into the macOS Keychain by opening a real browser for manual login, so other tools can call K-Ruoka's product API
---

# K-Ruoka Auth

## Overview

K-Ruoka's first-party API (`https://www.k-ruoka.fi/kr-api/...`) is what lets other tools in this repo look up real product prices. Reaching it needs a logged-in browser session plus Cloudflare clearance, and neither can be obtained without a real browser and a human.

This skill opens a visible Chrome window, waits while you log in by hand, harvests the resulting credentials, proves they work with one live API call, and stores them in the macOS Keychain.

Use this skill when:
- Setting up K-Ruoka access for the first time.
- A price-lookup tool reports an expired session or a Cloudflare challenge.
- You want to check whether the stored credentials are still valid.

**The password is never seen, typed, or stored by the automation.** Login happens in the browser window, by you, including any MFA prompt or Cloudflare challenge.

## What gets captured

| Item | Why it is needed |
|---|---|
| `session` cookie | The account session. Lasts roughly three months. |
| `cf_clearance` cookie | Cloudflare clearance. Short-lived, and bound to your IP and User-Agent. |
| `__cf_bm` cookie | Cloudflare bot management. Short-lived. |
| User-Agent string | Must be replayed verbatim, or `cf_clearance` stops matching. |
| `X-K-Build-Number` | Required header. A stale value gets HTTP 409 "client version is too old". |
| `storeId` | So downstream tools price the store you actually shop at. Taken from your Plussa favourite once you are logged in. |
| Store name | Cosmetic, so `status` reads `N123 (K-Citymarket Somewhere)` rather than a bare code. |

The two Cloudflare cookies are optional — Cloudflare only issues `cf_clearance` when it actually challenges the browser, so a capture without it is normal.

## Process

### Step 1: Run the capture

The script sits next to this SKILL.md. It is executable and resolves its own dependencies through `uv`, so there is nothing to install:

```bash
.claude/skills/kruoka-auth/kruoka-auth.py capture
```

The first run takes a minute while `uv` fetches Playwright. Later runs start immediately.

### Step 2: Drive the browser

A Chrome window opens at k-ruoka.fi. In that window:

1. **Log in.** The profile is persistent, so after the first time you usually already are.
2. Optionally search for a product, e.g. `maito`.

You do **not** need to pick a store. Once you log in, the site loads the favourite store from your Plussa profile and the capture reads it from there. Verified live: a signed-out capture recorded one store id, and signing in switched it to the account's favourite with no store interaction at all.

Pass `--store-hint N123` if you want the capture to fail rather than silently record the wrong shop.

The script polls in the background and closes the window itself once it has everything. It waits five minutes by default (`--timeout`).

### Why logging in actually matters

k-ruoka.fi hands a `session` cookie to anonymous visitors too, and product search answers them perfectly happily with normal prices. So neither a session cookie nor a successful search proves you are signed in — an earlier version of this skill was fooled by exactly that and captured a guest session.

What does prove it: the active basket echoes a `userInfo` block that stays completely blank until an account is attached. `capture` waits for that block to fill in before it will finish, and `status --validate` re-checks it.

This matters because **Plussa prices and offers are invisible to a guest session**, and those are the whole point of price-checking against your own account.

### Step 3: Confirm

On success it prints the captured store and build number, the result of a live validation call, and confirms the Keychain write. If validation fails, nothing useful was captured — read the error and run `capture` again.

## Commands

```bash
# Open Chrome, log in, harvest, validate, store
kruoka-auth.py capture

# Fail unless a specific store was the one selected
kruoka-auth.py capture --store-hint N190

# Report which credentials are present and how old they are
kruoka-auth.py status

# …and make one live API call to prove they still work
kruoka-auth.py status --validate

# Print a shell export block, for handing credentials to another process
kruoka-auth.py export-env > ~/.kruoka-env
```

## How other tools read the credentials

Credentials resolve in this order, and any one of the three is enough:

1. **Environment variables** — the contract every consumer relies on.
2. **A credentials file**, first match wins:
   - `<repo root>/.kruoka-env` — checked first, so a sandboxed agent confined to its workspace can reach it
   - `~/.kruoka-env`
   - `KRUOKA_ENV_FILE` overrides both
3. **The macOS Keychain** — a convenience for interactive use on this Mac.

**Environment variables are the contract.** Any consumer should read these and nothing else:

```
KRUOKA_SESSION       KRUOKA_CF_CLEARANCE  KRUOKA_CF_BM
KRUOKA_USER_AGENT    KRUOKA_BUILD_NUMBER  KRUOKA_STORE_ID
KRUOKA_STORE_NAME    KRUOKA_CAPTURED_AT
```

The Keychain is only a convenience for interactive use on macOS. `kruoka-auth.py` tries the environment, then a credentials file, then the Keychain, so the same code runs unchanged on a host that has none of the latter two.

### Handing credentials to a headless agent

On the same machine, or another host on the same network:

```bash
# same machine
kruoka-auth.py export-env > ~/.kruoka-env && chmod 600 ~/.kruoka-env

# another host, without the secret ever touching disk in between
kruoka-auth.py export-env | ssh user@host 'umask 077 && cat > ~/.kruoka-env'
```

Nothing further is needed. The tools find the file themselves, so the agent
needs no environment configuration, and refreshing credentials is a file
overwrite with **no restart** — environment variables, by contrast, are only
read once when a process starts.

**For a sandboxed agent** confined to its workspace, put the file at the repo
root instead. That path is checked first, and it is reachable when the home
directory is not:

```bash
kruoka-auth.py export-env \
  | ssh user@host 'umask 077 && cat > ~/workspace/cookbook/.kruoka-env'
```

`.kruoka-env` is gitignored, so it will not be committed or show up in
`git status`. It is still a live login to the account: keep it `0600` and
never copy it anywhere that syncs or backs up.

If you would rather set environment variables anyway:

```bash
set -a; . ~/.kruoka-env; set +a
```

A file readable by other users gets a warning on every run; keep it `0600`.

### What `cf_clearance` is actually bound to

Three things, not one:

- **The client IP** — credentials work from the same machine or home network, and will most likely be rejected from a different public IP.
- **The User-Agent** — replayed verbatim, which is why it is captured and exported alongside the cookies.
- **The TLS handshake fingerprint** — the one that catches people out. Measured from a single machine, single IP, single set of valid cookies: plain `curl` is challenged while `httpx` succeeds, and `curl_cffi` impersonating current Chrome succeeds where pretending to be Chrome 124 is refused.

That last point means an ordinary HTTP client can be accepted on one host and refused on another with nothing else different — in practice, `httpx` works on macOS and is challenged on Linux. Both tools therefore send requests through `curl_cffi` with `impersonate="chrome"`, which reproduces Chrome's fingerprint on any platform, falling back to `httpx` if no wheel is available for the host.

**A Cloudflare challenge from a second machine is not a stale-credentials problem.** Check whether the same credentials still validate from the Mac before re-capturing: if they do, the difference is the client, not the cookies.

## Requirements and portability

Read this first if you are adopting the skill into another repo.

| Needs | Why |
|---|---|
| `uv` on PATH | The script's shebang is `#!/usr/bin/env -S uv run --script`, and dependencies are declared inline (PEP 723). Nothing to pip-install; uv resolves and caches on first run. |
| Google Chrome installed | Playwright drives the real browser via `channel="chrome"`, so no bundled Chromium is downloaded. |
| macOS, for the Keychain only | `capture` and Keychain storage are macOS-only. Everything downstream reads environment variables or a credentials file, so consumers run anywhere. |

Two assumptions to check when relocating the file:

- **`REPO_ROOT` is derived as `Path(__file__).resolve().parents[3]`**, which assumes the script sits at `<repo>/.claude/skills/kruoka-auth/kruoka-auth.py`. Move it to a different depth and the repo-root credentials-file lookup points somewhere wrong. Adjust that constant.
- **The ignore rules live in the consuming repo's `.gitignore`, not in the skill folder.** Copying the skill alone will not bring them. Add:

  ```
  .claude/skills/kruoka-auth/.profile/
  .claude/skills/kruoka-auth/.profile.lock
  *.kruoka-env
  ```

  The first is a real Chrome profile holding a live logged-in session, roughly 30 MB. Committing it would publish the account.

The API details this was reverse-engineered against — endpoints, headers, the
`X-K-Build-Number` behaviour — are documented separately in Feaston's
`.agents/skills/kruoka-api/SKILL.md`.

## Defaults and constraints

- **Manual login only.** The script never fills in credentials. Do not add form-filling — it would put the password through the automation and would break on MFA and Cloudflare anyway.
- **Capture is never headless.** `cf_clearance` only exists because a real browser passed a challenge. A headless agent consumes credentials; it never captures them.
- **Dedicated Chrome profile** at `.claude/skills/kruoka-auth/.profile/`, mode `0700`, gitignored. It is separate from your everyday Chrome, so the two never conflict, and it keeps you logged in between captures.
- **Real Chrome**, via Playwright's `channel="chrome"`. No bundled Chromium is downloaded.
- **Keychain writes use the Keychain Services API** (the `keyring` package), not `security add-generic-password -w`, which would expose each secret in the process table.
- **`status` never prints secret values** — only presence, length, age, and the non-secret store id and build number. `export-env` does print secrets, by design; redirect it to a `0600` file.
- **Read-only downstream.** This credential is for looking up prices. Basket changes and checkout are out of scope.

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| "the session is still anonymous" | The window was open but you never logged in. A guest session is not enough. | Run `capture` again and complete the login. |
| "the session is anonymous" on validate | Stored credentials are a guest session. | Run `capture` again and log in. |
| "No `/kr-api/` call was seen" | The page never talked to the API. | Run `capture` again and search for a product before the window closes. |
| "Cloudflare challenged the request", **on the capturing machine** | `cf_clearance` is stale. | Run `capture` again and solve the challenge in the window. |
| "Cloudflare challenged the request", **on another host** | Almost certainly the TLS fingerprint, not the cookies. | Check the same credentials still validate on the capturing machine first. If they do, re-capturing will not help — see *What `cf_clearance` is actually bound to*. |
| HTTP 401/403 on validation | Session expired. | Run `capture` again. |
| "Build number … is stale" | The site shipped a new build. | Run `capture` again; the new build is picked up automatically. |
| "Could not launch Chrome" | Chrome missing, or a capture is already running. | Close the other window, or install Google Chrome. |
| "Another capture is already running" | A stale lock, or a genuine second run. | Finish or close the other browser window. |

## Expected maintenance

`cf_clearance` is short-lived, so expect to re-run `capture` every week or two. Because the Chrome profile is persistent, that is usually a ten-second job: the window opens already logged in, you search for a product, and it closes itself.
