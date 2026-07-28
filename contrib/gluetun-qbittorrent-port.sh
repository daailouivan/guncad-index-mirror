#! /bin/sh
set -eu

action="${1:-}"
case "$action" in
	up)
		port="${2:-}"
		interface="${3:-tun0}"
		case "$port" in
			"" | *[!0-9]*)
				echo "Gluetun supplied an invalid qBittorrent port: $port" >&2
				exit 2
				;;
		esac
		[ "$port" -ge 1 ] && [ "$port" -le 65535 ] || {
			echo "Gluetun supplied an out-of-range qBittorrent port: $port" >&2
			exit 2
		}
		payload="$(printf \
			'json={"listen_port":%s,"current_network_interface":"%s","random_port":false,"upnp":false}' \
			"$port" "$interface")"
		;;
	down)
		payload='json={"listen_port":0,"current_network_interface":"lo","random_port":false,"upnp":false}'
		;;
	*)
		echo "Usage: $0 up PORT [INTERFACE] | down" >&2
		exit 2
		;;
esac

endpoint="${QBITTORRENT_WEBAPI_URL:-http://127.0.0.1:8080}/api/v2/app/setPreferences"
attempt=1
while [ "$attempt" -le 120 ]; do
	if wget -q -O /dev/null --timeout=3 --post-data="$payload" "$endpoint"; then
		echo "qBittorrent accepted Gluetun port-forwarding state ($action)"
		exit 0
	fi
	sleep 1
	attempt=$((attempt + 1))
done

echo "qBittorrent did not accept Gluetun port-forwarding state ($action)" >&2
exit 1
