#! /bin/bash
# GunCAD Mirror Docker entrypoint
set -euo pipefail

# What user should we drop privs to if we're root?
targetuser="mirror"

#
# We set this envvar in the Dockerfile
# Please DO NOT override this so you can run it in your shell
#
if [ -z "${GUNCAD_IN_DOCKER:-}" ]; then
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
	mkdir -p /var/lib/logrotate
	chown -R "$targetuser": /data /home/"$targetuser" /var/lib/logrotate
	ls -alh /data
	echo "Pivoting to $targetuser"
	printf "Current args:"
	printf " %q" "$@"
	printf "\n"
	exec runuser -u "$targetuser" -- "$(realpath "$0")" "$@"
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
echo "  TZ:                            ${TZ:-Unset (you should fix this)}"
echo "  MIRROR_API_ENDPOINT:           ${MIRROR_API_ENDPOINT:-Default}"
echo "  MIRROR_API_MAX_PAGES:          ${MIRROR_API_MAX_PAGES:-Default}"
echo "  MIRROR_MAX_RELEASES_PER_RUN:   ${MIRROR_MAX_RELEASES_PER_RUN:-Unlimited}"
echo "  MIRROR_LBRY_URL:               ${MIRROR_LBRY_URL:-Default}"
echo "  MIRROR_BLACKLISTED_HANDLES:    ${MIRROR_BLACKLISTED_HANDLES:-Unset}"
echo "  MIRROR_ENABLE_WEBUI:           ${MIRROR_ENABLE_WEBUI:-Unset}"
echo "  MIRROR_RELEASE_MAX_SIZE:       ${MIRROR_RELEASE_MAX_SIZE:-Default}"
echo "  MIRROR_MIN_FREE_SPACE:         ${MIRROR_MIN_FREE_SPACE:-Default}"
echo "  MIRROR_DOWNLOAD_TIMEOUT:       ${MIRROR_DOWNLOAD_TIMEOUT:-Default}"
echo "  MIRROR_TORRENT_PIECE_LENGTH:   ${MIRROR_TORRENT_PIECE_LENGTH:-Default}"
echo "  MIRROR_TORRENT_TRACKERS:       ${MIRROR_TORRENT_TRACKERS:-Unset}"
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
mkdir -p /data/outbox
mkdir -p /data/releases
if ! [ -e ~/.local/share/lbry ]; then
	ln -s /data/lbry ~/.local/share/lbry
fi
# Copy in configs
mkdir -p ~/.local/share/lbry/lbrynet
cp /app/configfiles/daemon_settings.yml ~/.local/share/lbry/lbrynet/daemon_settings.yml

lbrynet_pid_file="/tmp/guncad-mirror-lbrynet.pid"
lbrynet_supervisor_pid=""

supervise_lbrynet() {
	set +e
	stop_requested=0
	child_pid=""

	# shellcheck disable=SC2329  # Called indirectly by the signal trap below.
	request_stop() {
		stop_requested=1
		if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
			kill -TERM "$child_pid" 2>/dev/null
		fi
	}

	wait_for_child() {
		while true; do
			wait "$child_pid"
			child_status=$?
			# A trapped signal interrupts wait before the child necessarily exits.
			# Wait again so lbrynet can finish its database and header flushes.
			if ! kill -0 "$child_pid" 2>/dev/null; then
				break
			fi
		done
	}

	trap request_stop TERM INT HUP

	while [ "$stop_requested" -eq 0 ]; do
		logrotate -f /etc/logrotate.d/lbrynet
		lbrynet start \
			--no-save-files \
			--no-share-usage-data \
			--save-blobs \
			--track-bandwidth \
			--use-upnp \
			> /data/log/lbrynet.log 2>&1 &
		child_pid=$!
		printf '%s\n' "$child_pid" > "$lbrynet_pid_file"
		wait_for_child
		rm -f "$lbrynet_pid_file"

		if [ "$stop_requested" -ne 0 ]; then
			break
		fi

		echo "lbrynet exited with status $child_status; restarting in 3 seconds"
		sleep 3 &
		child_pid=$!
		wait_for_child
		child_pid=""
	done
}

stop_lbrynet() {
	app_status=$?
	trap - EXIT INT TERM HUP

	if [ -n "$lbrynet_supervisor_pid" ] && kill -0 "$lbrynet_supervisor_pid" 2>/dev/null; then
		echo "Stopping lbrynet and flushing its persistent state"
		kill -TERM "$lbrynet_supervisor_pid" 2>/dev/null || true

		shutdown_deadline=$((SECONDS + 90))
		while kill -0 "$lbrynet_supervisor_pid" 2>/dev/null; do
			if [ "$SECONDS" -ge "$shutdown_deadline" ]; then
				echo "lbrynet did not stop within 90 seconds; forcing it down" >&2
				if [ -r "$lbrynet_pid_file" ]; then
					read -r child_pid < "$lbrynet_pid_file"
					kill -KILL "$child_pid" 2>/dev/null || true
				fi
				kill -KILL "$lbrynet_supervisor_pid" 2>/dev/null || true
				break
			fi
			sleep 1
		done
		wait "$lbrynet_supervisor_pid" 2>/dev/null || true
	fi

	rm -f "$lbrynet_pid_file"
	exit "$app_status"
}

trap stop_lbrynet EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

supervise_lbrynet &
lbrynet_supervisor_pid=$!

# Now move on to Python
python3 -m guncadmirror "$@" 2>&1 | tee -a /data/log/guncadmirror.log
