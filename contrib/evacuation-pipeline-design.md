# Odysee evacuation and torrent handoff design

Status: implementation checkpoint, July 2026

## Why this work moved to the front

[Julian Chandra's departure notice](https://x.com/julianpchandra/status/2077177321531093471) is not proof that Odysee will shut down. It is a credible warning that the people, funding, and plans behind the service have changed again, and that the remaining timetable is unknowable from outside the company.

That distinction matters. Panic would mean declaring the service dead on one post. The justified response is to stop treating Odysee as an indefinite storage guarantee while its API, LBRY peers, and Index records are still available.

The LBRY blockchain is not a safe continuity plan by itself. A small community cannot assume it can keep enough mining hardware online, defend a thinly mined chain from a majority attack, or operate the chain services needed by old clients. The useful part for evacuation is below the claim layer: stream descriptors, encrypted content-addressed blobs, peer discovery, and deterministic plaintext verification after assembly.

GESTALT remains a possible long-term replacement for the metadata and discovery plane. It is not the emergency path. Designing signed actor feeds, social discovery, trust traversal, metadata replication, moderation, and a replacement chain interface is a multi-month project before deployment and migration work begins. The short path uses software the community already runs: GunCAD Index, lbry-sdk, and ordinary BitTorrent clients.

## Decision

Use GunCAD Index as the migration catalog and eventual upload pane. Use GunCAD Mirror as a temporary LBRY extraction and verification tool. Use BitTorrent clients as the post-LBRY distribution layer.

| Component | Immediate responsibility | Post-Odysee responsibility |
| --- | --- | --- |
| GunCAD Index | Select releases, retain source metadata and checksums, expose API v2 | Creator identity, upload intake, torrent discovery, query-filtered RSS, public recovery data |
| GunCAD Mirror | Resolve LBRY stream descriptors, fetch blobs, assemble and verify files, generate torrents | Operate as a legacy migration utility until useful LBRY sources are exhausted |
| LBRY peers | Supply stream descriptors and encrypted blobs | Declining legacy source pool, with no assumption that the chain remains trustworthy |
| qBittorrent | Required bootstrap seeder for every torrent Mirror publishes | Main payload seeding and retrieval system, joined later by RSS subscribers and other clients |
| GESTALT | Design work only | Possible signed metadata, social discovery, and trust layer if the community still needs it |

This does make the Index a discovery and intake dependency at first. It does not make the Index the only holder of payload bytes. The recovery work below is required to keep that distinction real.

## Two separate identities

An LBRY `sd_hash` is not a plaintext file hash. It identifies one encrypted stream descriptor. That descriptor supplies the encryption key and ordered blob references needed to reconstruct a stream. The same plaintext file can have multiple valid descriptors and therefore multiple valid `sd_hash` values.

Mirror treats the identifiers separately:

- The Index release ID identifies the catalog record and migration job.
- The `sd_hash` identifies one LBRY acquisition route.
- The Index `origin.checksum` is the expected SHA-384 of the assembled plaintext.
- The torrent BTIH identifies one BitTorrent `info` dictionary.

This prevents a convenient transport locator from becoming accidental metadata authority. If a creator publishes a release, the creator's future signed feed or verified Index account is authoritative for that release's metadata. LBRY and BitTorrent hashes identify byte representations, not authorship.

## Implemented Mirror boundary

Mirror currently accepts GunCAD Index API v2 records only. A supported record must contain:

- `origin.platform` equal to `lbry`;
- a 40-character lowercase claim ID, also present as `origin.external_id`;
- a 96-character lowercase `origin.extra.sd_hash`;
- a 96-character lowercase plaintext SHA-384 in `origin.checksum`, or null for a legacy claim that did not publish one;
- a positive integer `origin.size`, or zero/null when the legacy claim did not publish a size;
- an LBRY source link; an HTTP or HTTPS Odysee link is retained when present but may be absent for LBRY-only claims;
- a non-empty release name and channel handle.

Printables and other origins are expected in API v2 responses. They are skipped at DEBUG level before LBRY-specific fields are evaluated. Malformed LBRY rows are logged as errors and isolated from valid rows. A missing legacy size or checksum is not treated as malformed: Mirror derives the actual values from the assembled artifact and retains null in `claimed_sha384`. This records a weaker evidence class without discarding the payload or overstating verification.

The implemented sequence is:

```text
Index API v2
    -> validate one LBRY origin
    -> register (release_id, sd_hash) in SQLite
    -> direct stream_get(sd_hash)
    -> fetch descriptor and encrypted blobs
       OR, after swarm failure and four-way identity validation,
       range-resume plaintext from Odysee CDN
    -> produce assembled plaintext
    -> verify exact size and SHA-384
    -> record SHA-256
    -> create BitTorrent v1 metainfo and magnet URI
    -> atomically write torrent and manifest
    -> awaiting_index
    -> add the exact BTIH to qBittorrent at the assembled payload path
    -> require complete forced-upload state and DHT or tracker discovery
    -> optional authenticated Index publication
    -> published, duplicate, rejected, conflict, or retrying
```

The acquisition state remains `awaiting_index` after publication. It means Mirror has a verified local payload, torrent, and source-evidence manifest. A second state machine records what happened at Index without converting a rejected API request into a missing local archive.

Publication is disabled unless `MIRROR_PUBLISH_ENABLED`, `MIRROR_PUBLISH_URL`, and `MIRROR_PUBLISH_TOKEN` are set. It also requires `MIRROR_QBITTORRENT_ENABLED`. This keeps an upgraded archive from writing to Index merely because the new image contains a client, while making an active initial seeder mandatory for every enabled publication.

## Direct stream acquisition

Stock lbry-sdk exposes claim-oriented `get` calls even though its internal stream classes can start from a descriptor hash. Mirror carries a small patch against the exact lbry-sdk v0.113.0 commit `a2da86d4b576bf316560a123cb568d8e1826d5b3`.

The patch adds `stream_get(sd_hash, ...)` to the daemon. It constructs a managed stream from the descriptor hash, uses the existing DHT, tracker, and fixed-peer downloader, reads the descriptor's encryption material, downloads the referenced blobs, decrypts the stream, writes the plaintext, and registers the stream in the existing file database.

The pinned SDK also resolves DHT bootstrap hosts independently and combines them with persisted peers on every empty-routing-table recovery. One dead DNS record can no longer discard the other bootstrap nodes, and stale persisted peers can no longer prevent a fresh bootstrap. A failed direct-descriptor request cancels its peer-search tasks and gives them a bounded drain period before returning, avoiding both abandoned asyncio tasks and an indefinitely blocked RPC during repeated acquisition timeouts.

Mirror first calls this RPC. It falls back to claim URI resolution only when the daemon reports JSON-RPC method-not-found. Even then, it compares the returned descriptor hash with the Index value and rejects claim drift.

An Index origin marked `origin.extra.lbry_only` is known to require authenticated owner access at Odysee. Mirror still attempts exact descriptor acquisition from LBRY peers, but never wastes an Odysee fallback slot or sends a request that cannot succeed.

This removes claim resolution from the normal data path. It does not yet remove every chain startup dependency from lbry-sdk: the daemon's file manager still waits on wallet startup, so a new data volume synchronizes chain headers before the RPC is ready. Persisting `/data/lbry` avoids paying that cost on each container start.

### Odysee recovery transport

Live full-catalog testing found two failure classes that repeated scans had not fixed: a stream descriptor with no responding LBRY peer and a known stream with a fixed set of missing content blobs. Odysee's public SDK proxy still resolved the descriptor-dead claim exactly, and its player CDN advertised the Index byte count through HTTP ranges. A separate stale Index record resolved to a newer descriptor and was rejected before transfer.

Mirror therefore has a last-resort Odysee transport. Before requesting payload bytes, Mirror requires the proxy's resolved claim ID, source `sd_hash`, plaintext size, and plaintext SHA-384 to equal the Index record. It then accepts an HTTPS stream URL only from `player.odycdn.com`, requires the URL path to identify the expected claim and descriptor prefix, requests an explicit byte range, and checks every `Content-Range` against the claimed total. Interrupted transfers remain in a `.odysee.part` file and resume from the exact byte count. The ordinary post-download size and SHA-384 verification still runs before torrent creation.

The fallback does not require an Odysee page link; Odysee's public proxy may still know an Index row marked LBRY-only. It is deliberately disabled per release when the Index has no independent size or checksum. In that case an Odysee response could not be tied to the expected plaintext strongly enough. A CDN-acquired manifest records `acquisition.transport` as `odysee-cdn`, the source URL, and the LBRY failure that caused the fallback. Earlier manifests omit the acquisition object and can be interpreted as `lbry`, because those builds had no CDN code.

HTTP 429 responses establish one cooldown shared by both CDN workers. Mirror honors `Retry-After`; without that header, repeated throttles wait 30, 60, 120, 240, and then 300 seconds. Range-request opening is serialized so two workers cannot probe at the same instant after a cooldown, but an established transfer does not hold that lock. Shutdown interrupts the wait and leaves the ranged partial intact.

## Verification and torrent rules

Mirror does not consider lbrynet's `finished` string sufficient proof. A completed acquisition must satisfy all of these checks:

- `file_list(sd_hash=...)` returns exactly one matching stream;
- status is `finished` and `blobs_remaining` is zero;
- the reported path resolves beneath the configured data root;
- the file exists and is nonempty;
- its size equals the Index size when the Index supplies one;
- a streaming SHA-384 equals the Index checksum when the Index supplies one.

Mirror always records the computed size, SHA-384, and SHA-256 before creating a single-file BitTorrent v1 torrent. Bencoding dictionaries are sorted bytewise. Piece hashes use SHA-1 because BitTorrent v1 requires it; the plaintext and torrent files retain SHA-384 and SHA-256 checksums outside that legacy field. For a legacy claim without an Index checksum, content-addressed descriptor and blob validation still protects the acquisition path, but the computed plaintext SHA-384 has no independent Index value to compare against.

Determinism is scoped to the same plaintext bytes, filename, piece length, and tracker list. Operators can choose different piece lengths or filenames and produce different valid BTIH values for the same plaintext. Index must therefore key the handoff by release ID and plaintext checksum, not assume one globally canonical torrent.

The default one-MiB piece length makes the observed 25,918,984-byte smoke payload a 25-piece torrent. Tracker URLs are optional and do not enter the `info` dictionary, but they do change the complete `.torrent` file checksum. With no trackers configured, the metainfo is intended for BitTorrent DHT.

## Durable state and failure behavior

Jobs are stored in SQLite using WAL mode and `synchronous=FULL`. The key is `(release_id, sd_hash)`, so a changed descriptor for an existing release becomes a distinct migration job instead of overwriting the old one.

```text
pending -> acquiring -> verified -> awaiting_index
                   +-> failed -> acquiring after backoff
excluded by policy -> acquiring after policy changes
```

qBittorrent reconciliation starts after `awaiting_index`:

```text
pending -> injecting -> green -> injecting after receipt expiry
                    +-> retrying -> injecting
                    +-> blocked -> injecting after repair or recheck
```

The supplied Compose stack builds `qbittorrentofficial/qbittorrent-nox:5.2.3-1` with a small configuration entrypoint. qBittorrent has its own persistent config volume and mounts Mirror's archive read-only at `/downloads`. Mirror parses the local torrent before calling the Web API, checks its BTIH, filename, and byte count against SQLite and the assembled payload, and adds it with the payload's parent as the save path. The client is forced into upload mode and reannounced.

A green receipt requires qBittorrent to return the same BTIH, a ledger-verified content and save path, the exact payload byte count, 100% progress, zero remaining bytes, a forced upload state, and either DHT nodes or a working tracker. qBittorrent stores one entry per BTIH. If two Mirror jobs have the same BTIH and SHA-384, either job's validated local path may satisfy both seed receipts; an arbitrary path remains a blocking conflict. The default receipt expires after 300 seconds. Publication SQL requires both `seeding_state=green` and a current receipt, so a stopped client closes the gate without rewriting acquisition or publication history. Network errors retry with backoff; path, size, incomplete-state, and local-artifact conflicts enter `blocked` and appear in notable events. Startup recovers an interrupted `injecting` row to `retrying`.

Publication starts only after that seed receipt:

```text
pending -> publishing -> published
                     +-> duplicate
                     +-> rejected
                     +-> conflict
                     +-> retrying -> publishing
```

The queue groups jobs by plaintext SHA-384. It submits the highest-popularity descriptor first, then processes other descriptors for the same bytes. Releases sharing one `(sd_hash, SHA-384, BTIH)` use one successful POST and receive the same canonical receipt. If two local rows somehow bind one descriptor to different BTIH values, Mirror submits both and lets Index retain the first permanent binding while returning an operator-visible conflict for the other. A retry deadline on the group leader blocks lower-popularity candidates, so a 429 response cannot hand the canonical race to whichever duplicate happened to run next.

Publication attempts are committed before the HTTP request. A process that stops in `publishing` recovers that row to `retrying` at startup. Network failures, HTTP 429, and most server errors retain a backoff deadline; 401, 403, 404, and 503 pause all publication because they identify a token, route, or server-configuration problem.

One release failure does not abort later releases. Failed jobs retain their typed error and retry deadline. Size and channel policy skips are terminal `excluded` jobs until the configured policy changes, and retain their exclusion reason without inflating the failure count. HTTP and JSON-RPC operations use bounded exponential retry. API pagination is bounded, rejects loops, and cannot leave the configured scheme and host. The process budgets two copies of each advertised payload plus a free-space reserve before starting acquisition. The stream timeout measures stalled time, not total transfer time: each decrease in `blobs_remaining` renews the deadline, so a large active download can finish without letting one dead stream hold the queue forever. If lbrynet exhausts its peer search and stops a stream, Mirror gives it one explicit resume attempt. A second stopped result fails the release immediately because no downloader remains active; the durable job ledger retries it during a later cycle.

Acquisition uses three bounded stages. `MIRROR_LBRY_CONCURRENCY=4` controls stream-descriptor acquisition, `MIRROR_ODYSEE_CONCURRENCY=2` controls independently verified CDN recovery, and `MIRROR_FINALIZE_CONCURRENCY=2` controls plaintext verification & torrent hashing. A future moves between executors; it never holds an LBRY worker while waiting for an Odysee worker. The default limits permit six network transfers at once without letting a pile of CDN fallbacks occupy the LBRY executor.

The scheduler keeps at most the sum of the three worker limits in flight. Every advertised payload reserves twice its size before work starts, covering encrypted blobs & assembled plaintext. If existing reservations consume the remaining budget, enumeration pauses on that release until a reservation is returned. If the filesystem itself lacks the required bytes, Mirror skips the release for the current cycle and records the exact available & required byte counts in notable events.

LBRY and Odysee workers each receive a thread-local HTTP session. Session cookies, connection pools, and streaming responses aren't shared between worker threads; runtime shutdown closes every session created by the pool.

`SIGTERM` cancels Index enumeration, retry waits, LBRY polling, Odysee range transfer, plaintext hashing, and torrent piece hashing. Network reads are capped at 60 seconds. An interrupted Odysee transfer flushes and syncs its `.odysee.part` file; an interrupted LBRY transfer remains registered with lbry-sdk instead of receiving `file_set_status(stop)`. Mirror leaves the job `acquiring` or `verified` without a failure deadline. The next scan reuses the partial bytes and repeats verification before writing the outbox.

Outbox identity is stable:

```text
/data/outbox/<release-id>/<sd-hash>/
    <plaintext-sha384>.torrent
    manifest.json
```

Plaintext identity is human-readable but collision-resistant within the release tree:

```text
/data/releases/<channel>/<release-name>-<sd-hash-prefix>/
    release.json
    <assembled payload>
```

The current fast idempotence check confirms that the payload, torrent, and manifest still exist. It does not hash an entire completed corpus on every four-hour scan. Scheduled bit-rot scrubbing is separate future work; a BitTorrent client's piece verification can cover the seeded copy in the meantime.

## Outbox contract

`manifest.json` uses schema name `guncad-mirror-publication-v1`. New manifests contain this information; manifests written before CDN fallback support omit `acquisition` and imply an LBRY acquisition:

```json
{
  "schema": "guncad-mirror-publication-v1",
  "status": "awaiting-index",
  "release": {
    "id": "<40-character release ID>",
    "name": "<release name>",
    "channel_handle": "<channel handle>",
    "url": "<historic HTTP source>",
    "url_lbry": "<historic LBRY source>"
  },
  "lbry": {
    "sd_hash": "<96-character descriptor hash>",
    "claimed_sha384": "<Index plaintext checksum>"
  },
  "acquisition": {
    "transport": "lbry or odysee-cdn",
    "source_url": "<null or verified Odysee player URL>",
    "lbry_failure": "<null or typed failure that caused fallback>"
  },
  "artifact": {
    "file_name": "<assembled filename>",
    "size": 123,
    "sha384": "<verified plaintext SHA-384>",
    "sha256": "<plaintext SHA-256>"
  },
  "torrent": {
    "file_name": "<assembled filename>",
    "piece_length": 1048576,
    "piece_count": 1,
    "btih": "<40-character BTIH>",
    "sha256": "<torrent-file SHA-256>",
    "magnet_uri": "magnet:?xt=urn:btih:...",
    "trackers": []
  }
}
```

Mirror rebuilds a compact wire manifest from this source record, its SQLite ledger, and a fresh parse of the torrent. It sends that JSON plus the `.torrent` file to `/api/v2/torrents/publish/` as two multipart fields only after qBittorrent passes the seed gate. Payload bytes stay on the Mirror node.

The response schema is `guncad-index-torrent-publication-v1`. Mirror accepts 200 `idempotent`, 201 `created` or `promoted`, and 409 `artifact_duplicate` only after the receipt matches the submitted descriptor, SHA-384, and BTIH. It stores the canonical SHA-384, BTIH, torrent URL, magnet URI, and winning release ID. A contradictory receipt pauses publication instead of recording success.

### Local publication checkpoint

The bounded localhost test on July 20, 2026 started with 74 `awaiting_index` jobs in `guncad-mirror-index-test-20260716`. Mirror attempted all 74 before lbrynet finished starting. GunCAD Index accepted 72 with HTTP 201 & created 72 `TorrentArtifact`, 72 `TorrentMetainfo`, and 72 `TorrentPublicationReceipt` rows. The other two requests received HTTP 400 `unknown_sd_hash`: the local Index no longer had a current LBRY origin for the V1.2 or V1.3 GP9-NEO9 Consolidated Megapack descriptor.

One accepted receipt was checked across both databases. Release `282f3c43908e1e0c514ce03a76d338872c02d076` retained the same descriptor hash, plaintext SHA-384, BTIH `eb33490202a2c177781782bac0eb941e3d90613a`, and winning release ID in Mirror & Index. Downloading the canonical torrent from Index produced SHA-256 `eb0e4b41df388829471a669fddbb46388f6114c5138ba66e674c230066fd8fbe`, byte-for-byte equal to Mirror's outbox torrent. Replaying the same multipart request returned HTTP 200 `idempotent`; the receipt count remained 72. A post-run archive audit found 74 valid artifacts, zero integrity issues, and no orphan manifests or torrents.

The qBittorrent checkpoint reused those 74 staged jobs without acquiring a payload or regenerating a torrent. They represent 73 distinct SHA-384 values and 73 BTIH values. The first pass imported the 73 swarm identities and marked 73 jobs green. The remaining job described the same GP9-NEO9 bytes and BTIH at a second verified release path; qBittorrent correctly retained only one entry for that BTIH. After Mirror learned to recognize the other ledger-verified path, a clean qBittorrent restart reconciled all 74 jobs as green in about five seconds. Mirror then sent the one deliberately reset publication only after reconciliation, and Index returned HTTP 200 `idempotent`. The final fast audit reported 74 valid artifacts, 74 green seed receipts, zero integrity issues, and no orphan manifests or torrents.

## Index handoff and continuity work

### Creator continuity before a shutdown

The highest-value work while Odysee remains writable is binding existing channels to accounts controlled on GunCAD Index:

1. Add Index logins with recoverable, low-friction authentication.
2. Verify Odysee channel ownership by asking the user to place a nonce in the public channel description.
3. Store the verified channel binding and the evidence needed to audit it later.
4. Give verified creators direct control over release tags, thumbnails, visibility, and requests for manual verification.

Those controls provide a reason to complete verification before an emergency. The nonce path stops working when Odysee channel editing stops, so it has a different deadline from bulk payload evacuation.

### Implemented torrent handoff

Index now accepts Mirror's compact evidence manifest and torrent metainfo without accepting the assembled payload. Its torrent application provides:

- authenticated publication with a shared high-entropy bearer;
- permanent `(sd_hash, SHA-384, BTIH)` receipts;
- SHA-384 artifact deduplication and popularity-led canonical election;
- checksum and size backfill for descriptor-authenticated legacy origins;
- direct torrent downloads, query-filtered RSS, and a bootstrap ZIP.

Mirror keeps the assembled file. Index stores the metainfo and the evidence needed to associate it with existing releases. This limits the publication request to a few megabytes even when the payload is tens of gigabytes.

Mirror now owns the first seeder. It won't publish an unseeded torrent and hope another operator appears before the swarm dies. Later seeders can join through Index downloads, the query-filtered RSS feed, or the bootstrap ZIP without running Mirror or lbrynet.

### Emergency feature flag

Index calls this switch `WINTER CONTINGENCY`. If Odysee becomes unavailable, it changes LBRY visibility and creator enrollment without disabling stored torrent metadata:

- accept creator uploads that have no surviving external origin;
- show torrent and magnet download controls;
- publish query-filterable torrent RSS feeds;
- advertise how to add a filtered feed to qBittorrent or another client.

The Index passes arbitrary search parameters through its torrent feeds. A seeder can select channels, tags, platforms, or other Index queries without new policy code in Mirror.

## Recovery from an Index outage

Torrent payload distribution removes one central byte host, but discovery can still collapse if every magnet and release mapping exists only in the live Index database. The emergency design is incomplete until Index publishes enough data to rebuild that mapping.

A recoverable torrent feed or snapshot should include, for each item:

- stable release ID, name, channel, and origin;
- plaintext size and legacy SHA-384;
- `sd_hash` when one exists;
- BTIH and magnet URI;
- a torrent-file URL and torrent SHA-256;
- publication and update timestamps;
- deletion or supersession state.

Periodic static snapshots should be easy to mirror without credentials. If the Index disappears, a snapshot plus surviving torrent seeders can reconstruct discovery. A snapshot cannot rescue payloads after the last seeder disappears, so this is a distribution plan, not a promise of permanent storage.

Creator-upload metadata eventually needs signatures outside the Index database if the project wants authorship to survive an Index loss. That is where GESTALT's signed actor feeds may return. It is not required to evacuate bytes from LBRY now.

## Mirror after Odysee

Mirror is intentionally LBRY-centric. Once Odysee and useful LBRY peers are gone, running it on every seeder would add a Python daemon, a frozen legacy lbrynet binary, chain state, and duplicate storage without improving BitTorrent.

The sensible steady state is:

```text
GunCAD Index filtered torrent RSS
    -> qBittorrent or another normal client
    -> selected payloads remain seeded
```

Mirror then becomes a migration and forensic tool, possibly run by one or a few archive operators against remaining LBRY blobs. Its extraction path does not need to become a polished end-user product if it is reliable, inspectable, and reproducible.

## Observed live checkpoint

The July 2026 smoke query intentionally mixed two LBRY channels with one Printables channel. A complete API v2 enumeration returned 74 rows: 69 supported LBRY origins and 5 Printables origins skipped as unsupported.

The first container smoke run selected Decimal's `MMMIIT v1` release and completed this path:

- descriptor hash acquisition without claim fallback;
- 13 payload blobs plus the stream descriptor;
- 25,918,984-byte assembled RAR;
- exact match to the Index SHA-384;
- independent SHA-256 calculation;
- 25-piece BitTorrent v1 metainfo at one MiB per piece;
- independent recalculation of BTIH from the raw bencoded `info` slice;
- durable torrent, manifest, release metadata, payload, and SQLite `awaiting_index` state in the named volume.

The first entrypoint did not wait for lbrynet during container exit. This mattered because the pinned SDK keeps its chain headers in memory and writes them only from `Headers.close()`. The mounted smoke volume therefore contained no header file at all, and each restart replayed roughly 850,000 tip headers.

The corrected supervisor sends lbrynet `SIGTERM`, waits for both database checkpoints and `Headers.close()`, and only then lets the container exit. The first fixed run wrote a 234,352,496-byte header file. The next cold start added one new tip header instead of replaying the chain and reached ready about 12 seconds after Mirror's first RPC probe. The SDK continued filling older missing checkpoint chunks in the background, and those chunks were persisted on the next clean exit.

That first checkpoint stopped at the local outbox. It predates the Index publication endpoint and the publication client described above.

## Unresolved work

The remaining work is operational:

1. Apply the seed gate and Index handoff to the archived corpus, then investigate every terminal rejection or evidence conflict.
2. Retrieve a published torrent from a second peer and measure behavior after the bootstrap operator goes offline.
3. Connect a separate qBittorrent client to a filtered Index feed and confirm unattended additions.
4. Decide moderation, takedown, access-control, and legal procedures before accepting bespoke uploads.
5. Establish a scheduled integrity-scrub policy and measure peer failures on later reconciliation runs.
6. Publish and independently mirror the recovery snapshots needed to rebuild torrent discovery after an Index outage.

Mirror has completed one full-corpus acquisition. The bounded qBittorrent and Index checkpoint proves the first-seeder handoff, but not long-term swarm health. Full-corpus seeding, second-peer retrieval, and recovery snapshots still need operational checks.
