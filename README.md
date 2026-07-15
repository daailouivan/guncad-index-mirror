# GunCAD Mirror

> [!WARNING]
> GunCAD Mirror has not had a stable release. Its current publication boundary is a local outbox. It does not upload torrents to GunCAD Index yet.

> [!WARNING]
> This software downloads and prepares redistribution metadata for 3D-printable firearm files. Possession, export, or distribution may be illegal where you live. The operator is responsible for complying with applicable law.

GunCAD Mirror is an evacuation bridge from LBRY to BitTorrent. It reads GunCAD Index API v2 releases, downloads each supported stream by its LBRY descriptor hash, verifies the assembled bytes against the Index checksum and size, and writes deterministic BitTorrent v1 artifacts. If the LBRY swarm cannot supply a claimed stream, Mirror can recover plaintext from Odysee's public CDN after matching the claim ID, descriptor, size, and hash.

It is not a replacement for a BitTorrent client. It does not seed the generated torrents. The intended post-Odysee seeder is an ordinary client such as qBittorrent or Transmission, eventually fed by a query-filterable torrent RSS feed from GunCAD Index.

## Current data path

For every valid API v2 release whose `origin.platform` is `lbry`, Mirror performs these steps:

1. Validate the claim ID, `sd_hash`, channel, and LBRY source link. Index payload size and SHA-384 values are validated when present. An Odysee HTTP link is retained when present but is not required.
2. Acquire the stream directly by `sd_hash` through the patched lbry-sdk daemon packaged in the container.
3. If LBRY acquisition exhausts its retries, optionally resolve the claim through Odysee and require the returned claim ID, `sd_hash`, plaintext size, and SHA-384 to match the Index before downloading from `player.odycdn.com`.
4. Assemble the plaintext file under `/data/releases`.
5. Compute the exact size, SHA-384, and SHA-256. When the Index supplies size or SHA-384 values, require an exact match.
6. Create deterministic single-file BitTorrent v1 metainfo and a magnet URI.
7. Atomically write the torrent and `manifest.json` under `/data/outbox`.
8. Mark the SQLite job `awaiting_index` and stop. No POST request is made.

Unsupported origins, including Printables, are skipped and visible in debug logs. LBRY-only releases that lack an Odysee page remain eligible because Mirror acquires them by `sd_hash`; the public proxy may or may not have a CDN copy. Some early LBRY claims contain neither source size nor source hash. Mirror preserves those descriptor-authenticated payloads and records computed values, but never sends them through the CDN fallback or presents them as independently corroborated. A malformed release is isolated from other rows on the same page. HTTP failures, pagination loops, cross-origin pagination, contradictory LBRY responses, checksum mismatches, and download timeouts are treated as errors rather than empty results or successful downloads.

## Quick start

Mirror needs a persistent `/data` volume. A complete LBRY evacuation stores both encrypted blobs and assembled plaintext, so budget close to twice the advertised Index payload corpus. Do not put `/data` on an ephemeral container layer.

The container runs as UID 1000 after creating the top-level data directories. Existing bind-mounted data must be writable by that UID. Startup does not recursively change ownership because doing so would walk the entire archive on every restart.

The packaged daemon serves cached LBRY blobs on TCP 5567 and participates in the LBRY DHT on TCP and UDP 4444. Forward those ports if this node should contribute data to other LBRY peers. The web UI is exposed on host port 8081 by the supplied Compose file. It refreshes every 10 seconds and reports the active release, acquisition transport, bytes or LBRY blobs remaining, average transfer rate for the current phase, and estimated time remaining.

```bash
docker compose --env-file guncad-mirror.env up -d
```

Podman Compose can use the same file:

```bash
podman compose --env-file guncad-mirror.env up -d
```

The default endpoint scans the full GunCAD Index API v2 catalog. Edit `MIRROR_API_ENDPOINT` in `guncad-mirror.env` before startup if you want a filtered mirror.

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

`contrib/test-docker.sh` builds the checkout and runs one release from a hard-coded, mixed-origin API query. The script refuses endpoints without a `query=` parameter. Its default Compose project is `guncad-mirror-smoke`, which keeps the test volume separate from a production Compose deployment and preserves LBRY headers between smoke runs.

```bash
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

## Data layout

| Path | Contents |
| --- | --- |
| `/data/lbry` | lbrynet configuration, chain headers, stream database, stream descriptors, and encrypted blobs. Preserve this directory between runs. |
| `/data/releases/<channel>/<release>-<sd-prefix>/` | Assembled plaintext payload and the raw API v2 `release.json`. |
| `/data/outbox/<release-id>/<sd-hash>/` | Deterministic `.torrent` file and `manifest.json` prepared for the future Index publication API. |
| `/data/mirror-state.sqlite3` | Durable job state, attempts, retry deadlines, verified hashes, torrent paths, BTIH values, and magnet URIs. |
| `/data/reports` | Consolidated archive audit output when `python -m guncadmirror.audit` is run. |
| `/data/log` | Mirror and lbrynet logs. |

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
| `MIRROR_ODYSEE_FALLBACK` | `true` | After LBRY acquisition fails, permit a range-resumable download from Odysee when claim ID, descriptor, size, and hash all match. |
| `MIRROR_ODYSEE_PROXY_URL` | `https://api.na-backend.odysee.com/api/v1/proxy` | Public Odysee SDK proxy used only for the verified fallback. Embedded credentials are rejected. |
| `MIRROR_BLACKLISTED_HANDLES` | empty | Comma-separated channel-handle prefixes. Replace the claim delimiter `:` with `#`, as in `@author#a`. |
| `MIRROR_RELEASE_MAX_SIZE` | `10737418240` | Maximum accepted payload size. Zero disables the limit. |
| `MIRROR_MIN_FREE_SPACE` | `5368709120` | Bytes reserved after budgeting for blobs and plaintext. |
| `MIRROR_LOOP_INTERVAL` | `14400` | Delay between completed scans. |
| `MIRROR_LBRY_STARTUP_TIMEOUT` | `300` | Deadline for required lbrynet components to start. |
| `MIRROR_DOWNLOAD_TIMEOUT` | `3600` | LBRY no-progress deadline. LBRY acquisition and Odysee CDN socket reads are capped at 60 seconds so `SIGTERM` remains bounded. |
| `MIRROR_DOWNLOAD_POLL_INTERVAL` | `2` | Delay between completion checks. |
| `MIRROR_RETRY_ATTEMPTS` | `5` | HTTP and JSON-RPC attempts per operation. |
| `MIRROR_RETRY_BACKOFF` | `2` | Initial exponential-backoff delay. |
| `MIRROR_ENABLE_WEBUI` | `false` | Serve the local status page on container port 5000. |
| `MIRROR_TORRENT_PIECE_LENGTH` | `1048576` | BitTorrent v1 piece length. Must be a power of two and at least 16 KiB. |
| `MIRROR_TORRENT_TRACKERS` | empty | Comma-separated HTTP, HTTPS, or UDP announce URLs. Empty creates trackerless metainfo. |

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
```

`awaiting_index` is terminal only because the Index upload endpoint does not exist yet. If a required local artifact disappears, Mirror rebuilds the job on the next scan. Failed jobs use exponential backoff and retain the last typed error in SQLite.

The process handles one release at a time. A failure does not discard completed jobs or stop later releases on the same Index scan. Operator cancellation is not a failed transition: work interrupted before payload verification remains `acquiring`, while work interrupted during torrent hashing remains `verified`. The next scan starts another attempt, reuses the local SDK stream or Odysee partial, and runs every verification step again.

On termination, Tini forwards the container signal. Mirror returns after the current network call or file chunk, then the entrypoint waits for lbrynet to checkpoint its databases and flush chain headers before exiting.

## Archive inventory and integrity audit

Mirror can consolidate the SQLite ledger and per-release manifests into four operator-facing files:

- `archive-summary.json`: counts, bytes, unique payloads, evidence classes, transports, and integrity totals.
- `archive-artifacts.csv`: one row per `awaiting_index` job, including hashes, paths, BTIH, magnet URI, and acquisition evidence.
- `archive-failures.csv`: typed acquisition failures and retry state.
- `archive-issues.csv`: missing files, path escapes, manifest contradictions, hash failures, unfinished jobs, and orphaned outbox files.

Run the fast audit after a scan reaches its sleep interval:

```bash
python -m guncadmirror.audit --data-dir /data
```

The fast pass validates the ledger, paths, file sizes, manifests, magnets, and SHA-256 of every torrent. It does not reread every assembled payload. Use `--rehash` for a bit-rot scrub that recomputes SHA-384 and SHA-256 over the complete plaintext corpus:

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

The torrent BTIH is computed from canonical bencoding of the BitTorrent v1 `info` dictionary. The outbox manifest also records SHA-256 of the plaintext and torrent file.

The manifest's `acquisition.transport` is `lbry` when descriptor and content blobs came from the swarm. It is `odysee-cdn` when the swarm failed and Odysee supplied plaintext that passed the independent Index size and SHA-384 checks. Manifests written before this field existed imply `lbry`; no CDN fallback existed in those builds.

## Known limits

- Mirror does not POST to GunCAD Index. The outbox is the handoff point for that future work.
- Mirror does not run a BitTorrent client or seed generated torrents.
- The patched RPC removes claim resolution from normal acquisition, but lbry-sdk's file manager still depends on wallet startup. A fresh data volume therefore pays the LBRY chain-header sync before downloads begin.
- When connected to an unpatched stock daemon, Mirror falls back to claim URI resolution only if `stream_get` is absent. It rejects the result if the resolved `sd_hash` differs from the Index value.
- The Odysee CDN fallback contacts Odysee directly and is useful only while its SDK proxy and player CDN remain online. Set `MIRROR_ODYSEE_FALLBACK=false` for a swarm-only run.
- Normal scans check that completed artifacts still exist. They do not perform a scheduled full-corpus bit-rot scrub.
- The container's patched legacy lbrynet build is currently amd64-only.

The immediate migration design and remaining Index work are recorded in [contrib/evacuation-pipeline-design.md](contrib/evacuation-pipeline-design.md).

## Support and license

For operator help, join the [GunCAD Index Matrix space](https://matrix.to/#/#guncad-index:matrix.org).

GunCAD Mirror is distributed under the [GNU Affero General Public License](LICENSE.md). The bundled IBM Plex fonts remain under the SIL Open Font License 1.1.
