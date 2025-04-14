* Added: The sdhash cache is now proactively cleaned to give admins a more accurate readout of how many files they're seeding
* Added: There's now a web UI you can enable, see the README for more information
* Added: We now use sdhash data from the Index if we can get it
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
