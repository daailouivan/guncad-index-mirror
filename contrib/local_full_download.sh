#! /bin/sh
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
bash "$SCRIPT_DIR"/test-docker.sh &
LOCAL_SITE_GENERATED=0
CONTAINER_NAME=guncad-mirror-guncad-mirror-1

while [ "$LOCAL_SITE_GENERATED" -eq 0 ]; do
	docker exec -it $CONTAINER_NAME sh -c "test -f /app/guncad_mirror_hugo/public/index.html" && LOCAL_SITE_GENERATED=1
	sleep 5
done

echo "SITE IS GENERATED"
docker cp $CONTAINER_NAME:/app/guncad_mirror_hugo/public/ $SCRIPT_DIR/../guncad_mirror_local_browser
docker stop $CONTAINER_NAME

#For some reason after finishing this script my commands would reliably disappear
#Running this fixes it
stty sane
