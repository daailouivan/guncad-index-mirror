# GunCAD Mirror

> [!WARNING]
> GunCAD Mirror has not had a stable release. Its current publication boundary is a local outbox. It does not upload torrents to GunCAD Index yet.

> [!WARNING]
> This software downloads and prepares redistribution metadata for 3D-printable firearm files. Possession, export, or distribution may be illegal where you live. The operator is responsible for complying with applicable law.

GunCAD Mirror is an evacuation bridge from LBRY to BitTorrent. It reads GunCAD Index API v2 releases, downloads each supported LBRY stream by its stream descriptor hash, verifies the assembled bytes against the Index checksum and size, and writes deterministic BitTorrent v1 artifacts.

It is not a replacement for a BitTorrent client. It does not seed the generated torrents. The intended post-Odysee seeder is an ordinary client such as qBittorrent or Transmission, eventually fed by a query-filterable torrent RSS feed from GunCAD Index.

## Current data path

For every valid API v2 release whose `origin.platform` is `lbry`, Mirror performs these steps:

1. Validate the claim ID, `sd_hash`, payload SHA-384, size, channel, and source links.
2. Acquire the stream directly by `sd_hash` through the patched lbry-sdk daemon packaged in the container.
3. Assemble the plaintext file under `/data/releases`.
4. Verify exact size and SHA-384. Mirror also records SHA-256 for downstream tooling.
5. Create deterministic single-file BitTorrent v1 metainfo and a magnet URI.
6. Atomically write the torrent and `manifest.json` under `/data/outbox`.
7. Mark the SQLite job `awaiting_index` and stop. No POST request is made.

Unsupported origins, including Printables, are logged and skipped. A malformed release is isolated from other rows on the same page. HTTP failures, pagination loops, cross-origin pagination, contradictory LBRY responses, checksum mismatches, and download timeouts are treated as errors rather than empty results or successful downloads.

## Quick start

Mirror needs a persistent `/data` volume. A complete LBRY evacuation stores both encrypted blobs and assembled plaintext, so budget close to twice the advertised Index payload corpus. Do not put `/data` on an ephemeral container layer.

The packaged daemon serves cached LBRY blobs on TCP 5567 and participates in the LBRY DHT on TCP and UDP 4444. Forward those ports if this node should contribute data to other LBRY peers. The web UI is exposed on host port 8081 by the supplied Compose file.

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

`contrib/test-docker.sh` builds the checkout and runs one release from a hard-coded, mixed-origin API query. The script refuses endpoints without a `query=` parameter. It uses a separate persistent volume so subsequent tests do not resync LBRY headers.

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
| `/data/log` | Mirror and lbrynet logs. |

Do not delete `/data/lbry` to clear a failed job. Delete or repair only the affected release/outbox artifacts, then let the next cycle retry. Removing LBRY state forces a chain-header sync and discards useful blobs.

## Configuration

All byte values are integers. All time values are seconds. Boolean values accept `true`, `false`, `1`, `0`, `yes`, `no`, `on`, and `off`, without regard to case.

| Variable | Default | Purpose |
| --- | ---: | --- |
| `MIRROR_API_ENDPOINT` | `https://guncadindex.com/api/v2/releases/?format=json&limit=100` | API v2 release list or filtered search. Embedded credentials are rejected. |
| `MIRROR_API_MAX_PAGES` | `1000` | Maximum pages followed in one scan. |
| `MIRROR_MAX_RELEASES_PER_RUN` | `0` | Maximum supported releases yielded per scan. Zero disables the cap. |
| `MIRROR_LBRY_URL` | `http://127.0.0.1:5279` | lbrynet JSON-RPC endpoint. |
| `MIRROR_BLACKLISTED_HANDLES` | empty | Comma-separated channel-handle prefixes. Replace the claim delimiter `:` with `#`, as in `@author#a`. |
| `MIRROR_RELEASE_MAX_SIZE` | `10737418240` | Maximum accepted payload size. Zero disables the limit. |
| `MIRROR_MIN_FREE_SPACE` | `5368709120` | Bytes reserved after budgeting for blobs and plaintext. |
| `MIRROR_LOOP_INTERVAL` | `14400` | Delay between completed scans. |
| `MIRROR_LBRY_STARTUP_TIMEOUT` | `300` | Deadline for required lbrynet components to start. |
| `MIRROR_DOWNLOAD_TIMEOUT` | `3600` | Deadline for one stream acquisition. |
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

The process handles one release at a time. A failure does not discard completed jobs or stop later releases on the same Index scan. Process termination waits for the current operation where possible, while Tini forwards container signals to both Mirror and lbrynet.

## What the hashes mean

The LBRY `sd_hash` addresses one encrypted stream descriptor. It is not the canonical identity of the plaintext file. The same plaintext can have multiple valid stream descriptors because encryption material and stream metadata can differ.

Mirror therefore keeps three separate identifiers:

- Index release ID: metadata/job identity.
- LBRY `sd_hash`: acquisition identity for one encrypted stream.
- Index `origin.checksum`: expected SHA-384 of the assembled plaintext.

The torrent BTIH is computed from canonical bencoding of the BitTorrent v1 `info` dictionary. The outbox manifest also records SHA-256 of the plaintext and torrent file.

## Known limits

- Mirror does not POST to GunCAD Index. The outbox is the handoff point for that future work.
- Mirror does not run a BitTorrent client or seed generated torrents.
- The patched RPC removes claim resolution from normal acquisition, but lbry-sdk's file manager still depends on wallet startup. A fresh data volume therefore pays the LBRY chain-header sync before downloads begin.
- When connected to an unpatched stock daemon, Mirror falls back to claim URI resolution only if `stream_get` is absent. It rejects the result if the resolved `sd_hash` differs from the Index value.
- Normal scans check that completed artifacts still exist. They do not perform a scheduled full-corpus bit-rot scrub.
- The container's patched legacy lbrynet build is currently amd64-only.

The immediate migration design and remaining Index work are recorded in [contrib/evacuation-pipeline-design.md](contrib/evacuation-pipeline-design.md).

## Support and license

For operator help, join the [GunCAD Index Matrix space](https://matrix.to/#/#guncad-index:matrix.org).

GunCAD Mirror is distributed under the [GNU Affero General Public License](LICENSE.md). The bundled IBM Plex fonts remain under the SIL Open Font License 1.1.
