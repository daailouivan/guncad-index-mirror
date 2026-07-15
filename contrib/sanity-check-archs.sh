#! /bin/sh
set -eu

docker images --format '{{.ID}}' | while IFS= read -r image_id; do
	architecture="$(docker image inspect --format '{{.Architecture}}' "$image_id")"
	printf '%s %s\n' "$image_id" "$architecture"
done
