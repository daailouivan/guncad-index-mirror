#! /bin/sh
set -e

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

docker="$(which docker 2>/dev/null || true)"
[ -z "$docker" ] && docker="$(which podman 2>/dev/null || true)"
[ -z "$docker" ] && {
	echo "No compatible Docker analogue found"
	exit 1
}
composefile="$SCRIPT_DIR/../docker-compose-build.yml"
[ -r "$composefile" ] || {
	echo "Could not read compose file: $composefile"
	exit 2
}

"$docker" compose -f "$composefile" --env-file "$SCRIPT_DIR"/../guncad-mirror.env up --build --force-recreate
