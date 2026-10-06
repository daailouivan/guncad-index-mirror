#
# GunCAD Mirror MSB Dockerfile
#
ARG python=3.14
ARG lbrynet=v0.113.0
ARG lbrynet_commit=a2da86d4b576bf316560a123cb568d8e1826d5b3

# STAGE 1: Building lbrynet
#
# Why Ubuntu 20.04? Because it dodges an OpenSSL issue
# In 2021 they just removed a cipher because ??? reasons ???
# https://github.com/openssl/openssl/issues/16994
#
# Once 20.04 goes out of style, we can look toward what it takes to upgrade,
# but honestly the bigger fish is that lbrynet is on Py3.8 still. Ugh.
#
FROM docker.io/ubuntu:20.04 AS lbrynet
ARG lbrynet
ARG lbrynet_commit
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_ROOT_USER_ACTION=ignore
# OH FUCK OFF DEBIAN
# How is tzdata STILL an interactive config???
ENV DEBIAN_FRONTEND=noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN=true
RUN	apt-get update && \
	apt-get install -y --no-install-recommends \
		build-essential \
		file \
		git \
		libffi-dev \
		libssl-dev \
		python3.8 \
		python3.8-dev \
		python3.8-venv \
		python3-protobuf \
		unzip \
		wget && \
	rm -rf /var/lib/apt/lists/*
COPY contrib/lbry-sdk-direct-sd.patch /tmp/lbry-sdk-direct-sd.patch
# Build LBRY. Note that we have to pull Py3.8(!) because they don't support
# anything newer. Which blows ass. Oh well.
RUN	mkdir /root/buildlbrynet && \
	cd /root/buildlbrynet && \
	git clone --branch "$lbrynet" --depth 1 https://github.com/lbryio/lbry-sdk && \
	cd lbry-sdk && \
	test "$(git rev-parse HEAD)" = "$lbrynet_commit" && \
	git apply /tmp/lbry-sdk-direct-sd.patch && \
	python3.8 -m venv venv && \
	. venv/bin/activate && \
	pip install \
		'pyinstaller==6.21.0' \
		'pyinstaller-hooks-contrib==2026.6' \
		'wheel==0.41.2' && \
	make install && \
	python -c "from inspect import getsource; from lbry.dht.node import Node; from lbry.extras.daemon.daemon import Daemon; from lbry.stream.managed_stream import ManagedStream; daemon_source = getsource(Daemon.jsonrpc_stream_get); join_source = getsource(Node.join_network); accumulate_source = getsource(Node._accumulate_peers_for_value); assert 'stream_get' in Daemon.callable_methods; assert 'created_stream' in daemon_source and 'asyncio.wait(cleanup_tasks, timeout=1.0)' in daemon_source; assert 'persisted + known_nodes' in join_source and 'socket.gaierror as err' in join_source; assert 'asyncio.wait(tasks, timeout=1.0)' in accumulate_source; assert hasattr(ManagedStream, 'start_saving'); assert 'or self.sd_hash' in getsource(ManagedStream.suggested_file_name.fget); assert 'sd hash' in getsource(ManagedStream.save_file)" && \
	pyinstaller --onefile --hidden-import ipaddress --name lbrynet lbry/extras/cli.py && \
	./dist/lbrynet --version

# STAGE 2: Building the app
FROM docker.io/python:$python-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_ROOT_USER_ACTION=ignore
ENV DEBIAN_FRONTEND=noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN=true
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN mkdir /app
WORKDIR /app
COPY requirements.txt /app/
# pip is only needed at build time. Its vendored dependencies (msgpack,
# setuptools/pkg_resources, urllib3, ...) lag behind upstream security fixes and
# get flagged by Trivy, so uninstall it from the venv once requirements are in.
RUN	pip install --upgrade pip && \
	pip install --no-cache-dir -r requirements.txt && \
	pip uninstall -y pip
COPY ./ /app/

# STAGE 3: Prod build
FROM docker.io/python:$python-slim AS prod
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV GUNCAD_IN_DOCKER=True
ENV PATH="/opt/venv/bin:$PATH"
# The base image ships a system pip whose vendored msgpack, setuptools
# (pkg_resources) and urllib3 carry HIGH CVEs with no fixed pip release yet.
# Nothing in the runtime image uses pip, so remove it instead of shipping it.
RUN	apt-get update && \
	apt-get upgrade -y && \
	apt-get install -y --no-install-recommends curl logrotate tini && \
	rm -rf /var/lib/apt/lists/* && \
	rm -rf /var/cache/apt/archives/* && \
	PIP_ROOT_USER_ACTION=ignore /usr/local/bin/python -m pip uninstall -y pip
RUN	adduser --disabled-password --gecos "" --uid 1000 mirror && \
	mkdir /app /data && \
	chown -R mirror: /app /data
COPY --from=builder /opt/venv /opt/venv
COPY start.sh /usr/local/bin/start-guncad-mirror
RUN chmod +x /usr/local/bin/start-guncad-mirror
COPY --from=lbrynet /root/buildlbrynet/lbry-sdk/dist/lbrynet /usr/local/bin/lbrynet
COPY --from=builder --chown=mirror /app /app
COPY configfiles/logrotate.conf /etc/logrotate.d/lbrynet
ARG commit_sha=master
ARG commit_tag=
ENV GUNCAD_COMMIT_SHA=$commit_sha
ENV GUNCAD_COMMIT_TAG=$commit_tag
WORKDIR /app
EXPOSE 5567
ENTRYPOINT [ "/usr/bin/tini", "-g", "--", "/bin/bash", "/usr/local/bin/start-guncad-mirror" ]
