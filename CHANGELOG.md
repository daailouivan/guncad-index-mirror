# Unreleased

* Replaced the legacy reflection loop with a durable LBRY-to-BitTorrent evacuation pipeline
* Added strict GunCAD Index API v2 parsing, bounded same-origin pagination, and clean handling of non-LBRY origins
* Added a pinned lbry-sdk patch for direct stream-descriptor acquisition without normal claim resolution
* Use the stream-descriptor hash as a deterministic filename when legacy descriptors omit both filename fields
* Made direct stream acquisition return after scheduling plaintext assembly so Mirror owns the full download deadline
* Added exact payload size and SHA-384 verification, recorded SHA-256, and deterministic single-file BitTorrent v1 generation
* Added a SQLite job ledger with retry backoff and an atomic local publication outbox
* Fail confirmed stopped LBRY streams after one resume attempt instead of idling until the stall deadline
* Keep lbrynet log writes in append mode so `copytruncate` rotation cannot create sparse holes
* Keep production logging at `INFO`; the bounded smoke test remains verbose
* Move image build metadata after runtime dependency layers so ordinary commits retain the package cache
* Accept LBRY-only Index rows without an Odysee HTTP link and silence expected non-LBRY skips at production log level
* Preserve legacy claims without advertised size/checksum while keeping computed and independently claimed evidence distinct
* Add a range-resumable Odysee CDN recovery transport that requires exact claim, descriptor, size, and plaintext-hash agreement after LBRY failure
* Add a read-only archive audit that emits summary, artifact, failure, and integrity-issue reports, with an optional full payload rehash
* Report the active release, transport, byte or blob progress, transfer rate, and estimated time remaining in the web UI
* Run four LBRY acquisitions, two Odysee fallbacks, and two local finalization jobs in separate bounded worker pools
* Coordinate Odysee CDN throttling across workers, honor `Retry-After`, and use a 30-to-300-second fallback cooldown
* Show every active job in the web UI and record release failures, fallback transitions, cancellation, and storage pressure in notable events
* Replaced Black and isort with Ruff and rebuilt CI around static checks, branch coverage, container builds, and scans
* Rebuilt the container entrypoint, persistent smoke test, operator status page, Compose configuration, and Unraid template
* Fixed container shutdown so lbrynet checkpoints SQLite and persists its in-memory header chain before exit
* Removed the recursive startup ownership walk and decoupled hourly log rotation from lbrynet restarts
* Changed stream timeouts to measure time without blob progress so large healthy downloads are not rejected by a wall-clock deadline
* Made termination signals cancel network acquisition and local hashing without deleting resumable bytes or recording a failed job
* Removed the optional assembly, pickle cache, and Index-failure claim-search paths; plaintext assembly is now required for verified torrent generation

* Fixed: Added blank mountpoint for /data, which should hopefully make OSX containers work right
* Added: More tracker announcement hosts and a script to pull a list from an authoritative source

# 0.4.1

* Fixed: API port bind issues on certain setups

# 0.4.0

* Fixed: Erroneous inclusions in docker containers (small stuff, just noise)
* Added: GunCAD Mirror logs are now saved to /data/log with all the others in addition to being displayed on stdout
* Added: We now fall back to LBRY if we can't talk to the Index
* Added: Unraid template (thanks crocs!)
* Added: We now limit the size of files we acquire from the Index. By default, anything over 10GB is skipped. At time of writing, that is 50 of the 7.8k releases

# 0.3.4

* Fixed: Files are now properly downloaded even with MIRROR_ASSEMBLE_FILES disabled
* Added: Version number is now shown in the title of the web UI

# 0.3.3

* Fixed: Bytes are displayed properly
* Fixed: Fixed (for real this time) logging newly-acquired files while MIRROR_ASSEMBLE_FILES is on
* Fixed: Expensive stats are now primed with non-None values and deferred until later, not blocking the main thread

# 0.3.2

* Fixed: Stats are now actually really for real initialized before the web UI.

# 0.3.1

* Fixed: Stats are now initialized before the web UI, fixing an ISE
* Fixed: Turning `MIRROR_ASSEMBLE_FILES` on no longer spams the log with erroneous "new file" messages

# 0.3.0

* Added: Web UI now has a notable events log, for things like updated releases
* Added: Web UI resources now implement cachebusting per-startup
* Changed: Boolean environment variables now need a "truthy" string like "True", "1", or "Enabled" to be considered set

# 0.2.0

* Added: There's now a web UI you can enable, see the README for more information
* Added: Docs now specify how to set the timezone in the container -- be sure and check it out so times are listed correctly
* Added: We now use sdhash data from the Index if we can get it
* Added: The sdhash cache is now proactively cleaned to give admins a more accurate readout of how many files they're seeding
* Added: There's now a framework for internal stats reporting
* Changed: Directory layout for `/data/mirror` is now much more sane, sorted by Author and then Release using human-readable names

# 0.1.2

* Fixed: urllib no longer vomits on startup
* Fixed: Third invocations and on now actually work and don't die because of symlink hell

# 0.1.1

* Added: Logo now resides in `/contrib`
* Fixed: We now send a User-Agent string with requests
* Fixed: We now ensure all LBRY components we need are started after each request, handling restarts more gracefully. There will still be one failure when lbrynet restarts.
* Fixed: Errors to obtain sdhash are now logged
* Changed: Timeout for lbrynet is now substantially longer

# 0.1.0

Initial release
