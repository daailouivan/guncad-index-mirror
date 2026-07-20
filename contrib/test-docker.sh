#! /bin/sh
set -e

# This URL is intentionally narrow. Never replace it with the unfiltered API
# endpoint in this smoke test: that turns a test into a several-hundred-GB job.
default_endpoint='https://guncadindex.com/api/v2/releases/?format=json&limit=50&query=channel%3A%22%40ciwielab%3Ab%22%20OR%20channel%3A%22%40Decimal_Dot%3Ad%22%20OR%20channel%3AMaverick0197'
MIRROR_SMOKE_API_ENDPOINT="${MIRROR_SMOKE_API_ENDPOINT:-$default_endpoint}"
MIRROR_SMOKE_PROJECT="${MIRROR_SMOKE_PROJECT:-guncad-mirror-smoke}"
MIRROR_SMOKE_ENV_FILE="${MIRROR_SMOKE_ENV_FILE:-guncad-mirror.env}"
MIRROR_ENV_FILE="$MIRROR_SMOKE_ENV_FILE"
export MIRROR_SMOKE_API_ENDPOINT
export MIRROR_ENV_FILE

case "$MIRROR_SMOKE_API_ENDPOINT" in
	*"query="*) ;;
	*)
		echo "Refusing to run a live smoke test without a scoped API query"
		exit 3
		;;
esac

docker="$(which docker 2>/dev/null || true)"
[ -z "$docker" ] && docker="$(which podman 2>/dev/null || true)"
[ -z "$docker" ] && {
	echo "No compatible Docker analogue found"
	exit 1
}
composefile="docker-compose-build.yml"
[ -r "$composefile" ] || {
	echo "Could not read compose file: $composefile"
	exit 2
}
[ -r "$MIRROR_SMOKE_ENV_FILE" ] || {
	echo "Could not read environment file: $MIRROR_SMOKE_ENV_FILE"
	exit 2
}

echo "Live-test endpoint: $MIRROR_SMOKE_API_ENDPOINT"
echo "Release cap: ${MIRROR_SMOKE_RELEASES:-1}"
echo "Compose project: $MIRROR_SMOKE_PROJECT"
echo "Service environment: $MIRROR_SMOKE_ENV_FILE"
"$docker" compose \
	-f "$composefile" \
	--project-name "$MIRROR_SMOKE_PROJECT" \
	--env-file "$MIRROR_SMOKE_ENV_FILE" \
	up \
	--build \
	--force-recreate \
	--abort-on-container-exit \
	--exit-code-from guncad-mirror
