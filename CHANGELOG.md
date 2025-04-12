# 0.1.1

* Added: Logo now resides in `/contrib`
* Fixed: We now send a User-Agent string with requests
* Fixed: We now ensure all LBRY components we need are started after each request, handling restarts more gracefully. There will still be one failure when lbrynet restarts.
* Fixed: Errors to obtain sdhash are now logged
* Changed: Timeout for lbrynet is now substantially longer

# 0.1.0

Initial release
