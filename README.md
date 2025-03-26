# GunCAD Mirror

**NOTICE**: We have yet to have a first release, this is all draft shit. Don't take it at its word.

GunCAD Mirror is a small piece of software that watches out for content on a GunCAD Index instance and mirrors it, offering it to the LBRY network for increased resiliency and redundancy.

## Quickstart

Spin up a container:

```bash
docker run -v guncad-mirror:/data registry.gitlab.com/guncad-index/mirror:latest
```

There's also a Docker Compose file if you want to `docker compose up` instead.

## Detailed Configuration

Here are the volume mountpoints you're probably interested in:

| Volume Mountpoint | Description |
| ----------------- | ----------- |
| `/data`           | All stored data from the instance |

And here are some envvars you can use to configure the instance:

| Environment Variable | Description | Default Value |
| -------------------- | ----------- | ------------- |
| `MIRROR_API_ENDPOINT` | The URL to the `releases` API endpoint of a GunCAD Index instance to monitor. The default value is the primary production instance, but you can configure this to point to a private/alternative/development instance. You can also add a query here to filter your results, if the instance supports it. | `https://guncadindex.com/api/releases` |
| `MIRROR_DISK_USAGE_PERCENT`  | The maximum amount of disk space the Mirror should use, as a percentage. | `85` |

## License

This software is distributed under the terms of the [GNU Affero General Public License](/LICENSE.md).
