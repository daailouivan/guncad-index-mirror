#! /bin/bash
# GunCAD Mirror Docker entrypoint
set -e

# What user should we drop privs to if we're root?
targetuser="mirror"

#
# We set this envvar in the Dockerfile
# Please DO NOT override this so you can run it in your shell
#
if [ -z "$GUNCAD_IN_DOCKER" ]; then
	echo "Don't run this in your shell -- see the test scripts in contrib"
	echo "This is an entrypoint for the Docker container"
	exit 1
fi

# If we get this far, we should be in Docker

# If we're root, take advantage before dropping privs
# When we drop privs we'll just reexec the script as the target user, specified above
if [ "$(id -u)" -eq "0" ]; then
	echo "Running as root -- doing some preambulatory configuration"
	echo "  Changing ownership of /data..."
	chown -R "$targetuser": /data /home/"$targetuser" /var/lib/logrotate
	ls -alh /data
	echo "Pivoting to $targetuser"
	echo "Current args: $@"
	exec su "$targetuser" -s /bin/bash -c "$(realpath "$0") $@"
fi

# If we're here, we're an unprivileged user and should start the app up
echo "Running as user: $(whoami) (UID $(id -u))"
if [ -n "$GUNCAD_COMMIT_TAG" ]; then
	export GUNCAD_COMMIT_REF="$GUNCAD_COMMIT_TAG"
elif [ -n "$GUNCAD_COMMIT_SHA" ]; then
	export GUNCAD_COMMIT_REF="$GUNCAD_COMMIT_SHA"
else
	export GUNCAD_COMMIT_REF="master"
fi
echo "Git ref of build:"
echo "  GUNCAD_COMMIT_REF:             ${GUNCAD_COMMIT_REF:-Unset}"
echo "Configuration variables:"
echo "  MIRROR_API_ENDPOINT:           ${MIRROR_API_ENDPOINT:-Unset??}"
echo ""
echo "If you have any questions or concerns, reach out:"
echo "  Source (and docs):             https://gitlab.com/guncad-index/mirror"
echo "  Twitter:                       https://x.com/theshittinator"
echo "  Matrix:                        https://matrix.to/#/#guncad-index:matrix.org"
echo ""
echo "Support the project on ko-fi: https://ko-fi.com/theshittinator"
echo ""

# Spin up lbrynet
mkdir -p ~/.local/share
mkdir -p /data/lbry
mkdir -p /data/log
mkdir -p /data/mirror
if ! [ -e ~/.local/share/lbry ]; then
	ln -s /data/lbry ~/.local/share/lbry
fi
# Copy in configs
mkdir -p ~/.local/share/lbry/lbrynet
cp /app/configfiles/daemon_settings.yml ~/.local/share/lbry/lbrynet/daemon_settings.yml
(
set +e
while true; do
	logrotate -f /etc/logrotate.d/lbrynet
	timeout \
		--preserve-status \
		--kill-after 60 \
		86400 \
		lbrynet start \
		--no-save-files \
		--no-share-usage-data \
		--save-blobs \
		--track-bandwidth \
		--use-upnp \
		> /data/log/lbrynet.log 2>&1
	sleep 3
done
) &

# Now move on to Python
python3 -m guncadmirror "$@"
