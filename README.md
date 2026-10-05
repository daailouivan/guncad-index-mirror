# GunCAD Mirror

> [!WARNING]
> GunCAD Mirror has not had a stable release. Index publication is opt-in and writes production records. Test the endpoint and bearer against a non-production Index before enabling it on an existing archive.

> [!WARNING]
> This software downloads and prepares redistribution metadata for 3D-printable firearm files. Possession, export, or distribution may be illegal where you live. The operator is responsible for complying with applicable law.

GunCAD Mirror is an evacuation bridge from LBRY to BitTorrent. It reads GunCAD Index API v2 releases, downloads each supported stream by its LBRY descriptor hash, verifies the assembled bytes against the Index checksum and size, and writes deterministic BitTorrent v1 artifacts. If the LBRY swarm can't supply a claimed stream, Mirror can recover plaintext from Odysee's public CDN after matching the claim ID, descriptor, size, and hash.

The supplied Compose stack runs qBittorrent beside Mirror. qBittorrent receives each generated torrent at the assembled payload's existing path, then seeds that file without downloading a second copy. Mirror won't publish a torrent to GunCAD Index until qBittorrent reports the same BTIH, a ledger-verified archive path and byte count, 100% completion, forced upload mode, and a working DHT or tracker path.

## Repository status

GitHub (`daailouivan/guncad-index-mirror`) is the working remote, and its default branch is `v2-migration`. The GitLab fork [gitlab.com/daailouivan/mirror](https://gitlab.com/daailouivan/mirror) is kept in sync on `v2-migration`. Upstream remains [gitlab.com/guncad-index/mirror](https://gitlab.com/guncad-index/mirror); this account does not push there.

The v1-to-v2 migration runner (`python -m guncadmirror.migration`) is on this branch and has been live-tested against a sample of the archive:

- It reads local LBRY stream records from `lbrynet.sqlite` instead of walking the SMB share.
- It extracts canonical torrents from the bootstrap archive into the API v2 outbox, and builds torrents for delisted or unindexed files that are still on disk.
- It skips empty 40-character hex directory stubs left by an interrupted update.
- It records migrated releases in `/data/mirror-state.sqlite3`. On a CIFS mount, SQLite falls back when POSIX byte-range locks fail. Pass `--nolock` if the share is mounted without `nobrl`.
- `audit` accepts `--target-prefix` so a host data directory can be checked against container paths stored as `/data/...`.

A sample audit of 5 artifacts reported 0 integrity issues, 0 orphan manifests, and 0 orphan torrents. The full archive run (about 12,060 local streams, 11,087 canonical bootstrap torrents) has not been started. Index publication stays opt-in.

Multi-source ingestion is next, not done. The release model still accepts only `origin.platform == "lbry"`. Printables and GitHub origins are skipped. Each new platform should acquire its own payload, then use the same checksum, torrent, outbox, and qBittorrent path.

## Current data path

For every valid API v2 release whose `origin.platform` is `lbry`, Mirror performs these steps:

1. Validate the claim ID, `sd_hash`, channel, and LBRY source link. Index payload size and SHA-384 values are validated when present. An Odysee HTTP link is retained when present but is not required.
2. Acquire the stream directly by `sd_hash` through the patched lbry-sdk daemon packaged in the container.
3. If LBRY acquisition exhausts its retries, optionally resolve the claim through Odysee and require the returned claim ID, `sd_hash`, plaintext size, and SHA-384 to match the Index before downloading from `player.odycdn.com`.
4. Assemble the plaintext file under `/data/releases`.
5. Compute the exact size, SHA-384, and SHA-256. When the Index supplies size or SHA-384 values, require an exact match.
6. Create deterministic, trackerless single-file BitTorrent v1 metainfo and a magnet URI.
7. Atomically write the torrent and `manifest.json` under `/data/outbox`.
8. Mark the acquisition job `awaiting_index`. This state means the local payload, torrent, and manifest are complete.
9. Add the torrent to qBittorrent at the assembled file's read-only mounted path. Reconcile its runtime tracker hints against Index policy & operator configuration, then record a short-lived green receipt only after qBittorrent reports upload mode and peer discovery.
10. When publication is enabled, send the manifest and torrent to the configured Index endpoint only while that green receipt remains current. Store the returned canonical SHA-384, BTIH, torrent URL, magnet URI, and winning release ID in SQLite.

Unsupported origins, including Printables, are skipped and visible in debug logs. LBRY-only releases that lack an Odysee page remain eligible because Mirror acquires them by `sd_hash`; the public proxy may or may not have a CDN copy. Some early LBRY claims contain neither source size nor source hash. Mirror preserves those descriptor-authenticated payloads and records computed values, but never sends them through the CDN fallback or presents them as independently corroborated. A malformed release is isolated from other rows on the same page. HTTP failures, pagination loops, cross-origin pagination, contradictory LBRY responses, checksum mismatches, and download timeouts are treated as errors rather than empty results or successful downloads.

## Quick start

Mirror needs a persistent `/data` volume. A complete LBRY evacuation stores both encrypted blobs and assembled plaintext, so budget close to twice the advertised Index payload corpus. Do not put `/data` on an ephemeral container layer.

The container runs as UID 1000 after creating the top-level data directories. Existing bind-mounted data must be writable by that UID. Startup does not recursively change ownership because doing so would walk the entire archive on every restart.

The packaged daemon serves cached LBRY blobs on TCP 5567 and participates in the LBRY DHT on TCP and UDP 4444. qBittorrent listens on TCP and UDP 6881. Forward those ports if this node should serve either network. The Mirror web UI is exposed on host port 8081. It reports active transfers, seed-gate state, Index publication state, and recent operator events. Its `/archive` page lists completed payloads, torrents, magnet links, and the last qBittorrent observation.

Set `MIRROR_QBITTORRENT_PASSWORD` in `guncad-mirror.env` before starting the production Compose stack. An empty value stops Compose before either container starts. qBittorrent's Web API stays on the private Compose network; the production file does not publish its control UI on the host.

```bash
docker compose --env-file guncad-mirror.env up -d
```

Podman Compose can use the same file:

```bash
podman compose --env-file guncad-mirror.env up -d
```

The default endpoint scans the full GunCAD Index API v2 catalog. Edit `MIRROR_API_ENDPOINT` in `guncad-mirror.env` before startup if you want a filtered mirror.

Index publication is disabled in the example configuration. Enabling it requires all three values below. The bearer belongs in the environment file, which should be readable only by the account that runs the container.

```text
MIRROR_PUBLISH_ENABLED="True"
MIRROR_PUBLISH_URL="https://guncadindex.com/api/v2/torrents/publish/"
MIRROR_PUBLISH_TOKEN="<high-entropy bearer>"
```

Publication also requires `MIRROR_QBITTORRENT_ENABLED=true`. The supplied Compose file sets that value and mounts Mirror's `/data` volume read-only at `/downloads` inside qBittorrent. Mirror's process receives Web API credentials; it never writes them to SQLite, the status page, or audit output.

### Routing qBittorrent through PIA

`docker-compose.gluetun.yml` puts qBittorrent inside a [Gluetun v3.41.1](https://github.com/qdm12/gluetun/releases/tag/v3.41.1) network namespace. Mirror, lbrynet, Odysee fallback requests, & Index publication keep their normal host connection. A VPN failure therefore stops BitTorrent traffic without taking the LBRY archive or Index scanner offline.

The VPN stack uses PIA OpenVPN because [Gluetun's PIA provider](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/private-internet-access.md) doesn't have native WireGuard support. Copy the ignored credential template and enter the service credentials accepted by PIA's manual OpenVPN endpoints:

```bash
cp .pia.env.example .pia.env
chmod 600 .pia.env
```

Docker Compose merges both environment files:

```bash
docker compose \
    --env-file guncad-mirror.env \
    --env-file .pia.env \
    -f docker-compose.gluetun.yml \
    up -d
```

`podman-compose` accepts only its last `--env-file` on some releases. Load both files into the calling shell instead:

```bash
set -a
. ./guncad-mirror.env
. ./.pia.env
set +a
podman compose -f docker-compose.gluetun.yml up -d
```

The default region is `CA Toronto`, selected from PIA's port-forwarding servers. Override it with `MIRROR_PIA_SERVER_REGIONS`; Gluetun's `PORT_FORWARD_ONLY=on` rejects a region without PIA port forwarding. The forwarded TCP & UDP port comes from PIA, not the host router. Don't forward host port 6881 or 6882 for this stack.

Gluetun writes its assigned port into the persistent `guncad-mirror-gluetun` volume and calls `contrib/gluetun-qbittorrent-port.sh` through its [port-forwarding hooks](https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/vpn-port-forwarding.md) whenever the lease comes up or goes down. The qBittorrent entrypoint reads the same file after an independent client restart, so it doesn't revert to port 6881 while Gluetun still owns another lease.

Gluetun's internal VPN recovery keeps the shared network namespace alive. qBittorrent loses egress while OpenVPN is down and resumes on the same forwarded port after Gluetun reconnects. Stopping or recreating the Gluetun container destroys that namespace; restart or recreate `gluetun` and `qbittorrent` as a pair. The firewall remains in the abandoned namespace until qBittorrent stops, so this failure mode blocks traffic instead of sending it through the host connection.

The qBittorrent Web UI binds only to `127.0.0.1:${MIRROR_QBITTORRENT_WEBUI_PORT:-8083}` on the host. Gluetun permits port 8080 on the private Compose interface for Mirror control, but not on the VPN interface. Localhost authentication bypass applies only inside the shared Gluetun/qBittorrent namespace; browser and Mirror requests still require the configured qBittorrent credentials.

Rootless Podman bind-mounts `/dev/net/tun` with its host SELinux label. The Gluetun service disables SELinux label separation for that container so Fedora permits access to the TUN device; it doesn't change the host-wide `container_use_devices` boolean. Gluetun still receives only `CAP_NET_ADMIN`, `/dev/net/tun`, its state volume, & the read-only port-sync script.

Gluetun runs at warning log level by default. Its informational startup summary prints the PIA username while masking the password. Set `MIRROR_GLUETUN_LOG_LEVEL=info` only when that username is acceptable in container logs.

## Tracker policy and torrent identity

Mirror keeps tracker configuration out of durable torrent identity. Every newly generated outbox torrent is trackerless, and every multipart upload to Index is trackerless. Two operators with the same payload name, bytes, & piece length therefore produce the same BitTorrent v1 `info` dictionary and BTIH without coordinating a tracker list.

Trackers belong to qBittorrent's mutable runtime state. On each Mirror cycle, the client fetches the public Index tracker policy with `If-None-Match`, stores the last valid document & ETag in `mirror-state.sqlite3`, and computes this exact set:

```text
(Index enabled trackers + MIRROR_TORRENT_TRACKERS) - Index blacklisted trackers
```

Mirror applies that set only to qBittorrent entries carrying both `MIRROR_QBITTORRENT_CATEGORY` and `MIRROR_QBITTORRENT_TAG`. It adds missing URLs, removes every other network tracker, ignores qBittorrent's DHT, PeX, & local-discovery pseudo-trackers, then reannounces the torrent. Manual tracker edits on a Mirror-owned qBittorrent entry last until the next five-minute seed check; put a persistent local URL in `MIRROR_TORRENT_TRACKERS`.

A policy outage does not stop acquisition, seeding, or publication. Mirror uses an endpoint-matched cached policy when one exists. If the node has never received a valid document from its configured endpoint, it adds operator trackers but does not remove existing qBittorrent hints; an unavailable endpoint is not an empty policy.

Legacy outbox torrents may contain `announce` or `announce-list`. Mirror can keep using those local files. Before publication, it removes only those top-level keys, preserves the exact encoded `info` dictionary byte span, recalculates the torrent SHA-256, & verifies that the BTIH did not change.

### Running the local development image

Build and tag the current checkout:

```bash
podman build --tag localhost/guncad-mirror:codex .
```

Then select that image and a named data volume:

```bash
MIRROR_IMAGE=localhost/guncad-mirror:codex \
MIRROR_DATA_VOLUME=guncad-mirror-data \
podman compose --env-file guncad-mirror.env up -d
```

To reuse the data produced by `contrib/test-docker.sh`, set `MIRROR_DATA_VOLUME` to the smoke volume's exact name. Never attach the same Mirror data volume to two running containers. Both lbrynet and Mirror keep SQLite databases there.

### Narrow live smoke test

`contrib/test-docker.sh` builds the checkout and runs one release from a hard-coded, mixed-origin API query. The script refuses endpoints without a `query=` parameter. Its default Compose project is `guncad-mirror-smoke`, which keeps the Mirror and qBittorrent test volumes separate from production and preserves both clients' state between smoke runs.

```bash
./contrib/test-docker.sh
```

Select a separate service environment without editing the checked-in example:

```bash
MIRROR_SMOKE_ENV_FILE=.mirror.env ./contrib/test-docker.sh
```

Reuse named bounded-test volumes by setting `MIRROR_SMOKE_DATA_VOLUME` and `MIRROR_SMOKE_QBITTORRENT_VOLUME`. The script refuses either name when it contains `full-corpus`. The smoke stack exposes qBittorrent's authenticated diagnostic UI only on `127.0.0.1:8083`; its default test username and password are both `mirror` unless overridden.

```bash
MIRROR_SMOKE_DATA_VOLUME=guncad-mirror-index-test-20260716 \
MIRROR_SMOKE_ENV_FILE=.mirror.env \
./contrib/test-docker.sh
```

Do not turn that script into a full-corpus runner. Use the production Compose file for a long-running backfill.

## Filtering the Index

GunCAD Index search parameters work on the API v2 releases endpoint. Build a search in the Index UI, then apply its `query` and `sort` parameters to `/api/v2/releases/`.

For example, this UI search:

```text
https://guncadindex.com/search?query=channel%3A%22%40Decimal_Dot%3Ad%22&sort=rank
```

becomes:

```text
https://guncadindex.com/api/v2/releases/?format=json&limit=100&query=channel%3A%22%40Decimal_Dot%3Ad%22&sort=rank
```

Mirror preserves and follows same-origin API pagination. `MIRROR_API_MAX_PAGES` and `MIRROR_MAX_RELEASES_PER_RUN` provide separate bounds for testing and staged deployment.

## Archive browser

Open `http://localhost:8081/archive` to browse jobs in `awaiting_index`. Search terms match release names, channel handles, & LBRY origin slugs without scanning the full API JSON stored for each job. Multiple terms must all match. Results are ordered by channel & release name, 50 rows per page.

Each result links to the assembled payload and locally generated `.torrent` file. The card shows qBittorrent's durable seed state, last observed upload state, DHT node count, and working tracker count. After Index publication, it also shows the elected canonical torrent and magnet URI. A duplicate release may retain a different local BTIH while pointing seeders at the Index winner for its shared SHA-384.

Payload responses support HTTP byte ranges, so an interrupted browser download can resume. Paths are read from the verified SQLite ledger; the server rejects unfinished jobs, missing files, & paths outside `/data` or `/data/outbox`.

The supplied Compose mapping binds port 8081 on every host interface. The browser has no login or TLS. Anyone who can reach that port can search & download the assembled files, so bind it to `127.0.0.1` or place it behind access control unless public downloads are intentional.

The outbox remains keyed by release ID & `sd_hash`. Names & LBRY slugs are display metadata; either can collide or change while `(release_id, sd_hash)` identifies one migration job.

## Upgrading from 0.4.1

Reuse the existing `/data` volume. Version 0.4.1 stored its lbrynet database, stream descriptors, & encrypted blobs under `/data/lbry`, which is the same location used by the evacuation build.

An old default installation wrote completed plaintext to `/dev/null` while retaining the blobs. Mirror now recognizes that stream record and calls lbrynet `file_save` with a destination under `/data/releases`. A complete cache requires no payload download from LBRY peers; lbrynet fetches only descriptor or content blobs absent from its local store. Assembly still writes the full plaintext once. Mirror then reads it once for SHA-384 & SHA-256 verification and once for BitTorrent piece hashing.

If `MIRROR_ASSEMBLE_FILES=True` was set in 0.4.1, an intact payload under `/data/mirror` is reused directly after its descriptor & size match the Index record. Mirror reads that file for verification & torrent generation but does not copy it into `/data/releases`.

The old pickle cache at `/data/sd_hash_cache.pkl` is ignored. The new SQLite ledger starts empty, so every selected release passes through verification & torrent generation once even when all of its bytes are already local.

An evacuation build from before qBittorrent integration already has `/data/releases`, `/data/outbox`, and `mirror-state.sqlite3`. Reuse that volume. Startup adds the seeding columns with `pending` defaults, then qBittorrent imports each existing torrent at its existing payload path. Mirror checks the torrent name, BTIH, and payload byte count before injection; it does not reacquire the LBRY stream or regenerate a valid outbox artifact. Keep the new qBittorrent config volume as well, because it contains the client's fast-resume records.

Tracker-bearing outbox files from an earlier build remain valid. Mirror derives their BTIH from the original `info` bytes, reconciles the qBittorrent copy as runtime state, & strips top-level tracker keys only from the Index upload. No payload reassembly or piece rehash is required.

## Data layout

| Path | Contents |
| --- | --- |
| `/data/lbry` | lbrynet configuration, chain headers, stream database, stream descriptors, and encrypted blobs. Preserve this directory between runs. |
| `/data/releases/<channel>/<release>-<sd-prefix>/` | Assembled plaintext payload and the raw API v2 `release.json`. |
| `/data/outbox/<release-id>/<sd-hash>/` | Deterministic `.torrent` file and source-evidence `manifest.json` used to build the Index request. |
| `/data/mirror-state.sqlite3` | Acquisition, seeding, and publication state; retry deadlines; verified hashes; local BTIH values; qBittorrent observations; endpoint-scoped tracker-policy cache; and canonical Index receipts. |
| `/data/reports` | Consolidated archive audit output when `python -m guncadmirror.audit` is run. |
| `/data/log` | Mirror and lbrynet logs. |

Compose also creates `guncad-mirror-qbittorrent` for qBittorrent configuration and fast-resume state. qBittorrent sees the Mirror data volume at `/downloads` with a read-only mount. It can read and seed payloads but can't rename, truncate, or delete the archived copy.

Do not delete `/data/lbry` to clear a failed job. Delete or repair only the affected release/outbox artifacts, then let the next cycle retry. Removing LBRY state forces a chain-header sync and discards useful blobs.

Stop Mirror through the container runtime instead of killing it. `SIGTERM` sets one cancellation event shared by Index enumeration, LBRY acquisition, Odysee range downloads, plaintext verification, and torrent piece hashing. Retry waits end immediately. A blocked HTTP or JSON-RPC request has a 60-second read timeout, so it cannot hold shutdown for the one-hour LBRY stall deadline.

Cancellation keeps partial work. Odysee flushes and calls `fsync()` on its `.odysee.part` file before returning. lbry-sdk keeps downloaded blobs and its stream record under `/data/lbry`; Mirror does not stop the SDK stream when the operator requests shutdown. The SQLite job remains `acquiring` or `verified`, without a failure or retry deadline, and the next scan resumes from those files.

lbry-sdk also buffers its chain-header file in memory and writes it during shutdown. Tini forwards the signal to the container process group, the supplied Compose files allow two minutes for shutdown, and the entrypoint gives lbrynet 90 seconds to flush before forcing it down. A `SIGKILL` or power loss can discard headers learned since the previous clean stop, even though completed blobs and Mirror's job ledger remain on disk.

## Configuration

All byte values are integers. All time values are seconds. Boolean values accept `true`, `false`, `1`, `0`, `yes`, `no`, `on`, and `off`, without regard to case.

| Variable | Default | Purpose |
| --- | ---: | --- |
| `MIRROR_API_ENDPOINT` | `https://guncadindex.com/api/v2/releases/?format=json&limit=100` | API v2 release list or filtered search. Embedded credentials are rejected. |
| `MIRROR_API_MAX_PAGES` | `1000` | Maximum pages followed in one scan. |
| `MIRROR_MAX_RELEASES_PER_RUN` | `0` | Maximum supported releases yielded per scan. Zero disables the cap. |
| `MIRROR_LBRY_URL` | `http://127.0.0.1:5279` | lbrynet JSON-RPC endpoint. |
| `MIRROR_LBRY_CONCURRENCY` | `4` | Maximum simultaneous LBRY stream acquisitions. |
| `MIRROR_ODYSEE_FALLBACK` | `true` | After LBRY acquisition fails, permit a range-resumable download from Odysee when claim ID, descriptor, size, and hash all match. Index origins marked `lbry_only` never use this fallback. |
| `MIRROR_ODYSEE_PROXY_URL` | `https://api.na-backend.odysee.com/api/v1/proxy` | Public Odysee SDK proxy used only for the verified fallback. Embedded credentials are rejected. |
| `MIRROR_ODYSEE_CONCURRENCY` | `2` | Maximum simultaneous Odysee CDN fallback downloads. |
| `MIRROR_FINALIZE_CONCURRENCY` | `2` | Maximum simultaneous plaintext verification and torrent-hashing jobs. |
| `MIRROR_BLACKLISTED_HANDLES` | empty | Comma-separated channel-handle prefixes. Replace the claim delimiter `:` with `#`, as in `@author#a`. |
| `MIRROR_RELEASE_MAX_SIZE` | `10737418240` | Maximum accepted payload size. Zero disables the limit. |
| `MIRROR_MIN_FREE_SPACE` | `5368709120` | Bytes reserved after budgeting for blobs and plaintext. |
| `MIRROR_LOOP_INTERVAL` | `14400` | Delay between completed scans. |
| `MIRROR_CYCLE_ERROR_INTERVAL` | `60` | Delay before retrying a scan aborted by an operational error. |
| `MIRROR_LBRY_STARTUP_TIMEOUT` | `300` | Deadline for required lbrynet components to start. |
| `MIRROR_DOWNLOAD_TIMEOUT` | `3600` | LBRY no-progress deadline. LBRY acquisition and Odysee CDN socket reads are capped at 60 seconds so `SIGTERM` remains bounded. |
| `MIRROR_DOWNLOAD_POLL_INTERVAL` | `2` | Delay between completion checks. |
| `MIRROR_RETRY_ATTEMPTS` | `5` | HTTP and JSON-RPC attempts per operation. |
| `MIRROR_RETRY_BACKOFF` | `2` | Initial exponential-backoff delay. |
| `MIRROR_ENABLE_WEBUI` | `false` | Serve the local status page on container port 5000. |
| `MIRROR_TORRENT_PIECE_LENGTH` | `1048576` | BitTorrent v1 piece length. Must be a power of two and at least 16 KiB. |
| `MIRROR_TORRENT_TRACKERS` | empty | Comma-separated operator tracker hints added to Mirror-owned qBittorrent entries. Index blacklisting wins. Durable torrent metainfo stays trackerless. |
| `MIRROR_TRACKER_POLICY_URL` | derived or disabled | Public Index tracker-policy endpoint. When empty, Mirror derives `../tracker-policy/` beside a configured `MIRROR_PUBLISH_URL`; with neither URL, policy fetching is disabled. The cache is scoped to the exact resulting URL. |
| `MIRROR_TRACKER_POLICY_TIMEOUT` | `15` | Read timeout for one conditional tracker-policy request. |
| `MIRROR_QBITTORRENT_ENABLED` | `false` | Reconcile staged torrents with qBittorrent. Must be true when Index publication is enabled. The supplied Compose files set it to true. |
| `MIRROR_QBITTORRENT_URL` | `http://qbittorrent:8080` | Private qBittorrent Web API endpoint. Embedded credentials are rejected. |
| `MIRROR_QBITTORRENT_API_KEY` | empty | qBittorrent 5.2 API key in `qbt_` format. Configure this or username/password, never both. |
| `MIRROR_QBITTORRENT_USERNAME` | empty | Web API username when API-key authentication isn't used. |
| `MIRROR_QBITTORRENT_PASSWORD` | empty | Web API password. The value is never included in status or audit output. |
| `MIRROR_QBITTORRENT_DATA_DIR` | `/downloads` | qBittorrent path corresponding to Mirror's data-volume root. Must be absolute. |
| `MIRROR_QBITTORRENT_TIMEOUT` | `15` | Web API read timeout. |
| `MIRROR_QBITTORRENT_READY_TIMEOUT` | `120` | Maximum wait for an injected torrent to report upload mode and peer discovery. |
| `MIRROR_QBITTORRENT_POLL_INTERVAL` | `2` | Delay between readiness checks during injection. |
| `MIRROR_QBITTORRENT_RECHECK_INTERVAL` | `300` | Lifetime of one green seed receipt before qBittorrent must be checked again. |
| `MIRROR_QBITTORRENT_CATEGORY` | `guncad-mirror` | Category assigned to imported torrents. |
| `MIRROR_QBITTORRENT_TAG` | `guncad-mirror` | Tag assigned to imported torrents. Commas are rejected. |
| `MIRROR_PUBLISH_ENABLED` | `false` | Enable authenticated Index publication. qBittorrent, URL, and token become required. |
| `MIRROR_PUBLISH_URL` | empty | Absolute HTTP(S) torrent publication endpoint. Embedded credentials are rejected. |
| `MIRROR_PUBLISH_TOKEN` | empty | Bearer sent only in the `Authorization` header. Whitespace and control characters are rejected. |
| `MIRROR_PUBLISH_CONCURRENCY` | `2` | Maximum SHA-384 groups published at once. Candidates within one group remain popularity ordered. |
| `MIRROR_PUBLISH_TIMEOUT` | `60` | Read timeout for one publication POST. |

The command-line entry point also accepts:

| Argument | Effect |
| --- | --- |
| `-v`, `--verbose` | Enable debug logging. |
| `--once` | Run one Index scan and exit. |
| `--max-releases N` | Override the environment release cap for this process. |

## Durable states and retries

Each job is keyed by `(release_id, sd_hash)`. The current state machine is:

```text
pending -> acquiring -> verified -> awaiting_index
                   +-> failed -> acquiring (after backoff)
excluded by policy -> acquiring (after policy changes)
```

`awaiting_index` is terminal for acquisition. If a required local artifact disappears, Mirror rebuilds the job on the next scan. Failed jobs use exponential backoff and retain the last typed error in SQLite. Size and channel-policy exclusions retain their reason without counting as failures; a later policy change makes them eligible for acquisition.

Seeding has its own durable state machine:

```text
pending -> injecting -> green -> injecting (after the receipt expires)
                    +-> retrying -> injecting
                    +-> blocked -> injecting after operator repair or recheck
```

`green` is a five-minute lease at the default setting. Mirror requires the exact local BTIH, a ledger-verified qBittorrent content and save path, payload size, 100% completion, zero remaining bytes, forced upload mode, a connected or firewalled transfer state, and either a nonzero DHT node count or a working tracker. qBittorrent stores one entry per BTIH, so jobs with the same BTIH and SHA-384 may share the path of either verified local copy. No unrelated path is accepted. Network failures enter `retrying`. A mismatched path, payload size, incomplete download state, or broken local torrent enters `blocked` and appears in notable events. A process exit during `injecting` recovers to `retrying` on startup.

Tracker reconciliation runs inside that same health check and does not add a second full-catalog scanner. After a successful mutation, Mirror takes a fresh qBittorrent observation before renewing the green receipt. A tracker API failure is recorded as degraded runtime discovery state, but a torrent that still has upload mode & DHT or another working tracker keeps its green receipt. The failed mutation is retried when that receipt expires.

Publication has a third state machine:

```text
pending -> publishing -> published
                     +-> duplicate
                     +-> rejected
                     +-> conflict
                     +-> retrying -> publishing
```

Publication candidates must have an unexpired green seed receipt. Network errors, HTTP 429, and most HTTP 5xx responses enter `retrying`. Mirror honors `Retry-After` and keeps lower-popularity descriptors for the same SHA-384 behind the failed leader. Rows with the same descriptor, plaintext hash, and BTIH share one POST; a divergent BTIH is submitted separately so Index can report the descriptor conflict. HTTP 401, 403, 404, and 503 pause the whole publisher because retrying every queued artifact cannot repair a bad token, wrong route, or disabled server endpoint. A process exit during `publishing` recovers to `retrying` on startup.

HTTP 200 and 201 responses end in `published`. An `artifact_duplicate` response ends in `duplicate` and stores the Index winner. Other HTTP 409 responses end in `conflict`; HTTP 400 and 413 responses end in `rejected`. These states never delete the local payload or torrent.

The scheduler has three separate worker pools: four LBRY acquisitions, two Odysee fallbacks, and two local finalization jobs by default. A release gives up its LBRY slot before entering the Odysee queue, so two slow CDN transfers don't reduce the four LBRY slots. Mirror reserves space for encrypted blobs & plaintext before submitting work. A release waits when another active reservation is the only reason it can't start; actual free-space shortage records a skip and an operator event.

Each worker handles one release per stage. A failure does not discard completed jobs or stop later releases on the same Index scan. Operator cancellation is not a failed transition: work interrupted before payload verification remains `acquiring`, while work interrupted during torrent hashing remains `verified`. The next scan starts another attempt, reuses the local SDK stream or Odysee partial, and runs every verification step again.

On termination, Tini forwards the container signal. Mirror returns after the current network call or file chunk, then the entrypoint waits for lbrynet to checkpoint its databases and flush chain headers before exiting.

## Index publication request

Mirror checks qBittorrent before it opens an Index request. If qBittorrent is unavailable, reports a conflicting torrent, or loses upload/discovery readiness, Mirror closes the publication gate and records the reason in SQLite and notable events.

After that check, Mirror sends one `POST` with `Authorization: Bearer <token>` and exactly two multipart fields. `manifest` is compact JSON under 64 KiB. `torrent` is the generated BitTorrent metainfo under 4 MiB. The assembled payload is not part of this request.

The wire manifest is rebuilt from the SQLite ledger and parsed torrent instead of forwarding the larger source-evidence manifest verbatim. Mirror parses the same strict single-file BitTorrent v1 subset as Index, checks the payload filename, length, magnet URI, & BTIH, then removes top-level `announce` and `announce-list` keys while preserving the original encoded `info` bytes. It recalculates the trackerless torrent SHA-256 and verifies the BTIH again before opening the HTTP connection. Source manifests written before acquisition evidence existed are sent as `acquisition.transport=lbry`.

Index responses use schema `guncad-index-torrent-publication-v1`:

| HTTP status | Mirror result |
| ---: | --- |
| `201 created` or `201 promoted` | Store the canonical receipt and mark the job `published`. |
| `200 idempotent` | Verify the receipt matches the submitted `sd_hash`, SHA-384, and BTIH, then mark `published`. |
| `409 artifact_duplicate` | Store the winning torrent URL and magnet, then mark `duplicate`. |
| Other `409` | Store the stable error code and any returned winner, then mark `conflict`. |
| `400` or `413` | Mark `rejected`; changing credentials won't repair invalid evidence. |
| Network error, `429`, or most `5xx` | Schedule a durable retry. |
| `401`, `403`, `404`, or `503` | Pause the publisher and retry after the operator-level cooldown. |

The client rejects a response whose receipt doesn't describe the exact submission. It also rejects malformed canonical URLs, magnets whose `xt` parameter doesn't name the returned BTIH, and a `canonical` flag that contradicts the returned artifact.

## Archive inventory and integrity audit

Mirror can consolidate the SQLite ledger and per-release manifests into five operator-facing files:

- `archive-summary.json`: counts, bytes, unique payloads, evidence classes, acquisition transports, seed states, publication states, and integrity totals.
- `archive-artifacts.csv`: one row per `awaiting_index` job, including local hashes, acquisition evidence, qBittorrent observations, publication attempts, terminal errors, and canonical Index receipt fields.
- `archive-failures.csv`: typed acquisition failures and retry state.
- `archive-exclusions.csv`: releases omitted by configured size or channel policy, including the durable reason.
- `archive-issues.csv`: missing files, path escapes, manifest contradictions, hash failures, unfinished jobs, and orphaned outbox files.

Run the fast audit after a scan reaches its sleep interval:

```bash
python -m guncadmirror.audit --data-dir /data
```

The fast pass validates the ledger, paths, file sizes, manifests, magnets, SHA-256 of every torrent, and internal consistency of stored qBittorrent receipts. It doesn't contact qBittorrent or reread every assembled payload. Use `--rehash` for a bit-rot scrub that recomputes SHA-384 and SHA-256 over the complete plaintext corpus:

```bash
python -m guncadmirror.audit --data-dir /data --rehash
```

Reports are written atomically under `/data/reports` by default. The audit opens Mirror's database read-only and never changes job state. A concurrent acquisition is safe, but it is reported as unfinished; run after a completed scan for a stable catalog census.

## What the hashes mean

The LBRY `sd_hash` addresses one encrypted stream descriptor. It is not the canonical identity of the plaintext file. The same plaintext can have multiple valid stream descriptors because encryption material and stream metadata can differ.

Mirror therefore keeps three separate identifiers:

- Index release ID: metadata/job identity.
- LBRY `sd_hash`: acquisition identity for one encrypted stream.
- Index `origin.checksum`: expected SHA-384 of the assembled plaintext when the legacy claim provides one.
- Torrent BTIH: identity of one BitTorrent `info` dictionary, including the payload filename and piece geometry.

The torrent BTIH is computed from canonical bencoding of the BitTorrent v1 `info` dictionary. The outbox manifest also records SHA-256 of the plaintext and torrent file.

The manifest's `acquisition.transport` is `lbry` when descriptor and content blobs came from the swarm. It is `odysee-cdn` when the swarm failed and Odysee supplied plaintext that passed the independent Index size and SHA-384 checks. Manifests written before this field existed imply `lbry`; no CDN fallback existed in those builds.

## Known limits

- The Mirror process controls qBittorrent but doesn't implement BitTorrent itself. Production Compose builds the pinned qBittorrent sidecar; the Unraid template requires a separate qBittorrent container with the same archive mounted read-only at `/downloads`.
- qBittorrent's production Web UI isn't published by Compose. The bounded development stack exposes it on `127.0.0.1:8083` for diagnosis.
- Tracker policy changes qBittorrent runtime state only. Torrent files already downloaded from Index remain byte-for-byte snapshots; clients add current trackers through Index-rendered magnets or local configuration.
- The patched RPC removes claim resolution from normal acquisition, but lbry-sdk's file manager still depends on wallet startup. A fresh data volume therefore pays the LBRY chain-header sync before downloads begin.
- When connected to an unpatched stock daemon, Mirror falls back to claim URI resolution only if `stream_get` is absent. It rejects the result if the resolved `sd_hash` differs from the Index value.
- The Odysee CDN fallback contacts Odysee directly and is useful only while its SDK proxy and player CDN remain online. Set `MIRROR_ODYSEE_FALLBACK=false` for a swarm-only run.
- Normal scans check that completed artifacts still exist. They do not perform a scheduled full-corpus bit-rot scrub.
- The container's patched legacy lbrynet build is currently amd64-only.

The immediate migration design and remaining Index work are recorded in [contrib/evacuation-pipeline-design.md](contrib/evacuation-pipeline-design.md).

## Support and license

For operator help, join the [GunCAD Index Matrix space](https://matrix.to/#/#guncad-index:matrix.org).

GunCAD Mirror is distributed under the [GNU Affero General Public License](LICENSE.md). The bundled IBM Plex fonts remain under the SIL Open Font License 1.1.
