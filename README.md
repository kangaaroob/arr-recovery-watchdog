# arr-recovery-watchdog

A small watchdog service that monitors Gluetun's internal health endpoint. When Gluetun
experiences an outage and subsequently becomes stably healthy again, the watchdog calls the
official Prowlarr, Sonarr, and Radarr **Test All Indexers** APIs to clear their lingering
circuit-breaker cooldowns.

## The problem

1. Gluetun/AirVPN temporarily stops passing traffic.
2. Prowlarr's indexers fail.
3. Prowlarr, Sonarr, and Radarr apply increasingly long circuit-breaker cooldowns to the
   failing indexers.
4. Gluetun eventually repairs the VPN.
5. The Arr applications remain disabled for hours, because nothing sends them an explicit
   "the network is back" signal.

This watchdog bridges steps 4 and 5. It does **not** attempt to repair the VPN, restart
containers, or touch any Arr database — it only calls the same "Test All Indexers" endpoints
you could click in each app's UI, which re-tests the indexers and clears their backoff timers.

## Why poll Gluetun's health endpoint instead of Docker health events?

Testing against a real Gluetun deployment showed that Docker's reported container health can
remain `healthy` throughout a VPN failure and recovery, while Gluetun's internal health
endpoint (`http://127.0.0.1:9999/`, HTTP 200 when healthy, HTTP 500 when unhealthy)
transitions accurately. Docker events are therefore not a reliable trigger, and the watchdog
polls the endpoint directly instead. It never touches `/var/run/docker.sock`.

The endpoint only listens on localhost inside Gluetun's network namespace, so the watchdog
runs with `network_mode: "service:gluetun"` and reaches it at `127.0.0.1:9999`. Do not expose
the health port on the host or the Docker network.

## The recovery state machine

```
                 unhealthy poll
  MONITORING ───────────────────────► OUTAGE
      ▲                                  │ healthy poll
      │                                  ▼
      │                             STABILIZING ◄─── any unhealthy poll in
      │                                  │           STABILIZING resets to OUTAGE
      │        continuously healthy for  │
      │        HEALTHY_STABILITY_SECONDS │
      │                                  ▼
      │                        cooldown active? ──yes──► COOLDOWN_WAIT
      │                                  │no                  │ cooldown expires,
      │                                  ▼                    │ still healthy
      │                             RECOVERING ◄──────────────┘
      │                                  │ run the test-all sequence
      │       every pending app          │
      │       accepted its test-all      │ any app failed (or Gluetun turned
      └──────────────────────────────────┤ unhealthy mid-sequence)
                                         ▼
                                    COOLDOWN_WAIT — retries only the still-
                                    pending apps once the cooldown expires
```

Any unhealthy poll in STABILIZING, COOLDOWN_WAIT, or RECOVERING returns the machine to
OUTAGE.

- **MONITORING** — Gluetun is healthy and no outage is pending. The steady state.
- **OUTAGE** — at least one unhealthy poll was observed (HTTP 500, any non-200 status,
  connection refusal, timeout, DNS failure, or a malformed response). A single bad poll is
  enough to be remembered. No Arr APIs are called while unhealthy.
- **STABILIZING** — Gluetun returned to HTTP 200 after an outage. The watchdog requires it to
  stay healthy continuously for `HEALTHY_STABILITY_SECONDS` (default 60 s); any failed poll
  during this window resets the timer.
- **COOLDOWN_WAIT** — a recovery is pending (either not yet attempted, or attempted with some
  applications failing) but a recovery attempt started within the last
  `ACTION_COOLDOWN_SECONDS` (default 30 min). The recovery (re)fires the moment the cooldown
  expires — provided Gluetun is still healthy.
- **RECOVERING** — a recovery attempt is due or in progress, with the list of applications
  still pending recorded. The phase is persisted, so a crash mid-recovery is treated as an
  unresolved outage on restart rather than a completed recovery.

The recovery sequence: POST Prowlarr's `/api/v1/indexer/testall`, wait
`PROWLARR_SETTLE_SECONDS` (default 10 s) so Prowlarr can refresh its indexer state, then POST
Sonarr's and Radarr's `/api/v3/indexer/testall`. Any 2xx response counts as success. Each
application is attempted independently — one failing never blocks the others — and failed
calls are retried `API_RETRY_COUNT` times with bounded exponential backoff (2 s, 4 s, 8 s, …
capped at 30 s).

A recovery only counts as **complete** when every pending application accepted its test-all.
Applications that failed stay pending; they are retried after the cooldown expires (health
permitting), and only the still-pending applications are re-tested — successful calls are
not repeated. Gluetun's health is re-checked before each application and during retry
backoff waits; if it turns unhealthy mid-sequence, the rest of the sequence is abandoned and
the machine returns to outage handling.

The attempt timestamp and the RECOVERING phase are persisted **before** any API call is
made, so failures can never cause rapid repeated attempts, and a crash mid-recovery can
never be mistaken for success. If the state file cannot be written, the recovery is
**deferred** — it runs on the next healthy poll after persistence succeeds again — because
acting on non-durable state could fire duplicate recoveries after a restart.

### Timekeeping and restarts

- Stabilization is measured with **monotonic time**, immune to wall-clock jumps.
- The recovery cooldown is stored as a **UTC wall-clock timestamp** so it survives restarts.
- State is persisted to `/data/state.json` atomically (temp file + fsync + rename), and only
  on meaningful transitions — never on every healthy poll.
- A missing state file is normal (first run). A corrupt or unreadable state file is logged
  clearly, quarantined to `state.json.corrupt`, and replaced with a genuinely conservative
  assumption: an **unresolved outage**. That may cause one unnecessary test-all after stable
  health, but it can never silently drop a recovery that was needed. The service does not
  crash.
- At startup the watchdog probes the state directory with a real write and **refuses to
  start (exit code 3)** if it is not writable — running without durable state would break
  the guarantees above.
- Restarting the watchdog while Gluetun is healthy and no outage was recorded performs **no**
  retest. If an unresolved outage was recorded before the restart — including a recovery
  that was in progress or partially failed — the watchdog conservatively restarts the
  stabilization window (it cannot prove health was continuous while it was down) and then
  recovers normally, re-testing only the applications that were still pending.

## Configuration

Everything is configured through environment variables. All have sensible defaults.

| Variable | Default | Description |
| --- | --- | --- |
| `GLUETUN_HEALTH_URL` | `http://127.0.0.1:9999/` | Gluetun's internal health endpoint. |
| `PROWLARR_URL` | `http://127.0.0.1:9696` | Prowlarr base URL (same namespace as Gluetun). |
| `SONARR_URL` | `http://sonarr:8989` | Sonarr base URL. Set to `""` to disable Sonarr. |
| `RADARR_URL` | `http://radarr:7878` | Radarr base URL. Set to `""` to disable Radarr. |
| `POLL_INTERVAL_SECONDS` | `5` | How often the health endpoint is polled. |
| `HEALTHY_STABILITY_SECONDS` | `60` | Continuous health required after an outage before recovery. |
| `PROWLARR_SETTLE_SECONDS` | `10` | Wait between Prowlarr's test-all and Sonarr/Radarr's. |
| `ACTION_COOLDOWN_SECONDS` | `1800` | Minimum time between recovery sequences. |
| `HTTP_CONNECT_TIMEOUT_SECONDS` | `5` | HTTP connect timeout (also the health-poll response timeout). |
| `HTTP_RESPONSE_TIMEOUT_SECONDS` | `60` | Response timeout for Arr API calls (test-all can be slow). |
| `API_RETRY_COUNT` | `3` | Additional retries per Arr API call, with bounded backoff. |
| `STATE_FILE` | `/data/state.json` | Persistent state location. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `DRY_RUN` | `false` | Monitor and log, but never send POST requests. |
| `PROWLARR_API_KEY_FILE` | – | Path to a file containing the Prowlarr API key (recommended). |
| `SONARR_API_KEY_FILE` | – | Path to a file containing the Sonarr API key. |
| `RADARR_API_KEY_FILE` | – | Path to a file containing the Radarr API key. |
| `PROWLARR_API_KEY` / `SONARR_API_KEY` / `RADARR_API_KEY` | – | Direct API keys, for development only. The `_FILE` variant always takes precedence. |

Prowlarr is required: a missing Prowlarr API key is a clear configuration error (exit code 2).
Sonarr and Radarr are optional: if an app has no API key it is skipped with a warning, and
setting its URL to an empty string disables it silently.

Secrets never appear in logs. A redaction filter additionally scrubs any configured API key
from every log line as defense in depth, and URLs are logged with credentials stripped.

## Obtaining the Arr API keys

In each application's web UI: **Settings → General → Security → API Key**. Prowlarr, Sonarr,
and Radarr each have their own key.

## Creating secret files

From your Compose project directory:

```sh
mkdir -p secrets
printf '%s' 'your-prowlarr-api-key' > secrets/prowlarr_api_key
printf '%s' 'your-sonarr-api-key'   > secrets/sonarr_api_key
printf '%s' 'your-radarr-api-key'   > secrets/radarr_api_key
chmod 700 secrets
chmod 600 secrets/*
```

A trailing newline in the file is fine — the watchdog strips surrounding whitespace. Keep the
`secrets/` directory out of version control (this repository's `.gitignore` already does).

## Development: tests, linting, type checks

Requires Python 3.12+.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

ruff check src tests    # lint
mypy                    # type-check (strict)
pytest                  # full test suite (fully mocked; touches no real services)
```

## Continuous integration

Every pull request, push to `main`, and manual dispatch runs the CI workflow
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): Ruff, strict Mypy, the full Pytest
suite, and a Docker image build that is **never published**. CI runs with a read-only
`GITHUB_TOKEN`, has no access to runtime Arr API keys, the homelab, or any registry
credential, and cannot publish packages. Superseded runs for the same branch or pull request
are cancelled automatically.

## Releases and the published image

Container images are published to GitHub Container Registry:

```text
ghcr.io/kangaaroob/arr-recovery-watchdog
```

### Publishing a release

1. Bump `[project].version` in `pyproject.toml` and merge to `main`. This is the version's
   single source of truth — the running application reports it via package metadata, and the
   release workflow refuses to publish a tag that doesn't match it.
2. On GitHub: **Releases → Draft a new release**, create a tag in the form `vMAJOR.MINOR.PATCH`
   (e.g. `v1.2.3`) targeting `main`, and publish. For a prerelease, use a semver prerelease
   tag such as `v1.2.3-rc.1` **and** check the "Set as a pre-release" box.
3. Publishing the release triggers [`.github/workflows/release.yml`](.github/workflows/release.yml),
   which validates the tag, re-runs Ruff/Mypy/Pytest at the exact tagged commit, and only then
   builds and pushes the image with Docker Buildx, provenance attestation, and an SBOM.
   Nothing is published if any validation or test fails.

The workflow authenticates with the automatic `GITHUB_TOKEN` — no Docker Hub account, no
manually created PAT, and no custom publishing secret is required or accepted. Runtime
Prowlarr/Sonarr/Radarr API keys remain homelab-only secrets and are never stored in GitHub
Actions. A `workflow_dispatch` re-run is available for transient failures, but only for a tag
that already belongs to a published GitHub Release.

### Image tags

A stable release `v1.2.3` publishes:

| Tag | Meaning |
| --- | --- |
| `1.2.3` | Exact release — **recommended for deployments** |
| `1.2` | Newest patch release within 1.2 |
| `1` | Newest release within major version 1 |
| `latest` | Newest stable release |
| `sha-<commit>` | The exact source commit |

A prerelease such as `v1.2.3-rc.1` publishes only `1.2.3-rc.1` and `sha-<commit>` — it never
updates `latest`, `1`, or `1.2`. The rolling tags are recomputed against every existing
release tag at publish time, so publishing (or re-running) an older stable release can never
move `latest` away from a newer one.

### Package visibility (first release only)

GHCR packages are **private on first publication**. To make the image publicly pullable, the
repository owner must, once: GitHub profile → **Packages** → `arr-recovery-watchdog` →
**Package settings** → **Danger Zone** → **Change visibility** → **Public**. Note that making
a GHCR package public is **irreversible** — it cannot be made private again.

### Pulling the image

Once public, no authentication is needed:

```sh
docker pull ghcr.io/kangaaroob/arr-recovery-watchdog:1.0.0
```

While the package is still private, authenticate with a classic PAT that has **only** the
`read:packages` scope:

```sh
echo "$GHCR_READ_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
```

## Building the Docker image locally

Deployments should normally use the published GHCR image, but a local build works exactly as
before:

```sh
docker build -t arr-recovery-watchdog .
```

The image runs as a non-root user, supports a read-only root filesystem, writes persistent
data only under `/data` and temporary data only under `/tmp`, contains no secrets, and stops
cleanly on SIGTERM. The base image is pinned by digest for reproducible builds; Dependabot
keeps the pin current.

## Compose integration

See [`compose.example.yml`](compose.example.yml) for a complete example. Key points:

- The service consumes the published image pinned to an explicit release version:

  ```yaml
  image: ghcr.io/kangaaroob/arr-recovery-watchdog:1.0.0
  ```

  To upgrade, edit the version to the new release and run:

  ```sh
  docker compose pull arr-recovery-watchdog
  docker compose up -d arr-recovery-watchdog
  ```

  Pinning the exact version keeps upgrades deliberate; the rolling `1.0` / `1` / `latest`
  tags exist if you prefer them. To build from a local checkout instead, replace `image:`
  with `build: ./arr-recovery-watchdog`.
- `network_mode: "service:gluetun"` puts the watchdog inside Gluetun's network namespace, so
  Gluetun's health endpoint and Prowlarr are reached via `127.0.0.1`. A service using
  `network_mode: "service:..."` **cannot** declare its own `networks:` or `ports:` sections.
- Sonarr and Radarr are reached by their Compose service names. That resolution and routing
  happens through *Gluetun's* namespace, so it works when Sonarr/Radarr share a Compose
  network with Gluetun. If they live on a different subnet, add it to Gluetun's
  `FIREWALL_OUTBOUND_SUBNETS`.
- **Namespace lifecycle caveat:** if Gluetun is recreated, the watchdog's network namespace
  goes away with it and the watchdog must be restarted. `depends_on: { gluetun: { restart:
  true } }` covers Compose-initiated recreations (e.g. `docker compose up -d` after a Gluetun
  image update). After recreating Gluetun outside Compose, restart the watchdog manually.
- `read_only: true`, a `tmpfs` on `/tmp`, a named volume on `/data`, and
  `no-new-privileges:true` keep the container locked down. The watchdog exposes no ports and
  serves nothing.

## Deploying safely: start with DRY_RUN

Set `DRY_RUN: "true"` (and optionally `LOG_LEVEL: DEBUG`) for the first deployment:

```sh
docker compose pull arr-recovery-watchdog
docker compose up -d arr-recovery-watchdog
docker logs -f arr-recovery-watchdog
```

In dry-run mode the watchdog monitors Gluetun and walks the full state machine, but logs
`[DRY RUN] ... would POST ...` instead of calling the Arr APIs. Once the transitions look
right, remove `DRY_RUN` and re-deploy.

## Verifying behavior with logs

Expected lines at INFO level:

```
... watchdog.main Initial Gluetun health: healthy (HTTP 200)
... watchdog.machine Startup: Gluetun healthy; baseline established, no action needed
... watchdog.machine Outage detected: Gluetun health check failed
... watchdog.machine Gluetun healthy again after outage; requiring 60s of continuous health before recovery
... watchdog.machine Gluetun stable for 60s; initiating Arr recovery for Prowlarr, Sonarr, Radarr
... watchdog.arr Starting Arr recovery sequence: Prowlarr, Sonarr, Radarr
... watchdog.arr Prowlarr: test-all accepted (HTTP 200)
... watchdog.arr Waiting 10s for Prowlarr to settle before testing Sonarr, Radarr
... watchdog.arr Sonarr: test-all accepted (HTTP 200)
... watchdog.arr Radarr: test-all accepted (HTTP 200)
... watchdog.arr Arr recovery sequence complete: Prowlarr=ok Sonarr=ok Radarr=ok
... watchdog.machine Recovery complete: every pending application accepted test-all
```

Per-poll details are at DEBUG level. All timestamps are UTC.

## Manually forcing a Gluetun health failure (intentional, disruptive)

To test the full loop, hold the VPN tunnel down from inside the Gluetun container. A single
`ip link set tun0 down` is often healed by Gluetun's own health loop *before the watchdog's
next poll*, so the outage is never observed; repeat the command to hold the tunnel down long
enough to be seen:

```sh
# Hold the tunnel down for ~60 s so the watchdog reliably observes the outage.
for i in $(seq 1 12); do
  docker exec gluetun ip link set tun0 down
  sleep 5
done
```

**Warning:** this genuinely interrupts VPN traffic for every container in Gluetun's
namespace (Prowlarr, qBittorrent, …) until Gluetun's own health loop restores the tunnel.
Only do this intentionally, in a maintenance window. It is deliberately *not* part of this
project's automated test suite — the automated tests are fully mocked and never contact the
homelab.

Watch the watchdog logs: you should see the outage recorded, stabilization begin once Gluetun
recovers, and — after 60 s of stable health — a recovery sequence (or a `[DRY RUN]` line).

## Implications of sharing Gluetun's network namespace

- The watchdog has no network identity of its own: no ports, no `networks:` section, and its
  outbound traffic is subject to Gluetun's firewall and routing (which is what lets it reach
  `127.0.0.1:9999` and Prowlarr directly).
- If Gluetun is down entirely, the watchdog's polls fail — which is correctly interpreted as
  "unhealthy" and handled by the state machine. The watchdog keeps running.
- The watchdog's lifetime is coupled to Gluetun's namespace (see the lifecycle caveat above).

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `test-all returned HTTP 401` | Wrong API key for that app, or the secret file contains the wrong value. Verify against Settings → General → Security in the app's UI. |
| `test-all returned HTTP 500` (or 400) | The app reached its indexers but some tests failed — often the indexers themselves are still recovering, or genuinely broken. Check the app's System → Status page and indexer list. The app stays **pending**: the watchdog retries it (and only it) after the cooldown expires. |
| `Recovery incomplete: ... still pending` | One or more applications did not accept test-all. The failed applications are retried automatically after the cooldown, provided Gluetun stays healthy. |
| `Recovery is due but the state file cannot be written` | `/data` stopped accepting writes (full disk, remounted read-only, volume problem). The recovery is deferred until state can be persisted again — fix the volume. |
| Exits immediately with code 3 | The state directory failed the startup write probe. Check the `/data` volume mount and its permissions (the container runs as UID 65532). |
| Health poll: connection refused | Gluetun's health server isn't reachable: the watchdog is treated as being in an outage. If it never recovers, confirm `network_mode: "service:gluetun"` is set and Gluetun's health server is on `127.0.0.1:9999` (Gluetun default `HEALTH_SERVER_ADDRESS`). |
| Prowlarr: connection refused | Prowlarr is not in Gluetun's namespace, or a non-default port. `PROWLARR_URL` must be `http://127.0.0.1:9696` when Prowlarr shares Gluetun's namespace. |
| Sonarr/Radarr unreachable (DNS failure or timeout) | The service name isn't resolvable/routable from Gluetun's namespace. Ensure Sonarr/Radarr share a Compose network with Gluetun; if they're on another subnet, add it to Gluetun's `FIREWALL_OUTBOUND_SUBNETS`. |
| Watchdog exits immediately with code 2 | Configuration error — the log's `Configuration error: ...` line says exactly what to fix (most commonly a missing Prowlarr API key or an unreadable secret file). |
| `State file ... is corrupt` | The previous state was quarantined to `state.json.corrupt` and the watchdog conservatively assumed an unresolved outage (at worst one redundant test-all after stable health) — safe, but worth investigating the volume if it recurs. |

## Project layout

```
.github/workflows/ci.yml       Lint + type-check + test + unpublished Docker build
.github/workflows/release.yml  Tag validation, tests, GHCR publish with attestation
.github/dependabot.yml         Weekly Actions/pip/Docker dependency updates
Dockerfile                     Hardened container build (non-root, read-only friendly)
compose.example.yml            Example Compose integration with secrets
pyproject.toml                 Packaging, ruff, mypy, pytest configuration
src/arr_recovery_watchdog/
  config.py                    Environment parsing, validation, secret files
  health.py                    Gluetun health client
  state.py                     Persistent state + atomic StateStore
  machine.py                   The recovery state machine (pure logic, no I/O)
  arr.py                       Arr API clients + recovery sequence runner
  clock.py                     Injectable clock with interruptible waits
  logs.py                      Logging setup + secret redaction
  main.py                      Wiring, signal handling, poll loop
tests/                         Fully mocked test suite (no live services)
```

## Design notes and boundaries

- Version 1 deliberately has no Prometheus metrics, notifications, or UI.
- The watchdog never restarts containers, never touches Docker, never modifies Gluetun or any
  Arr database, and never selects VPN servers.
- Retry backoff constants (base 2 s, cap 30 s) are fixed in code; the retry count is
  configurable via `API_RETRY_COUNT`.
- Exit codes: `0` after a clean shutdown (SIGTERM/SIGINT), `2` for configuration errors,
  `3` when the state directory fails the startup write probe.
- Both HTTP clients are created with `trust_env=False`, so inherited proxy environment
  variables can never redirect health polls or API calls carrying Arr keys.
- Health monitoring is synchronous with recovery: while a recovery sequence runs, ordinary
  polling pauses, but Gluetun's health is re-checked before each application and during every
  retry backoff, and an unhealthy result aborts the sequence. A failure-and-full-recovery of
  Gluetun that fits entirely inside a single settle/backoff wait can still go unobserved;
  the aborted applications remain pending and are retried, so no recovery is lost.
