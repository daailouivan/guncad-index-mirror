# GunCAD Mirror

**NOTICE**: We have yet to have a first release, this is all draft shit. Don't take it at its word.

GunCAD Mirror is a small piece of software that watches out for content on a GunCAD Index instance and mirrors it, offering it to the LBRY network for increased resiliency and redundancy.

## Quickstart

Spin up a container:

```
# Example container name, replace later
docker run -v guncad-mirror:/data foobar
```

There's also a Docker Compose file in the repo if you want to `docker compose up` instead.

## Detailed Configuration

Here are the volume mountpoints you're probably interested in:

| Volume Mountpoint | Description |
| ----------------- | ----------- |
| `/data`           | All stored data from the instance |

And here are some envvars you can use to configure the instance:

| Environment Variable | Description | Default Value |
| -------------------- | ----------- | ------------- |
| `MIRROR_SITE_URL`    | The GunCAD Index instance to monitor. The default value is the primary production instance, but you can configure this to point to a private/alternative/development instance. | `https://guncadindex.com` |
| `MIRROR_DISK_USAGE_PERCENT`  | The maximum amount of disk space the Mirror should use, as a percentage. | `85` |

## License

This software is distributed under the terms of the [GNU Affero General Public License](/LICENSE.md).
