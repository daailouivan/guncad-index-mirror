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
