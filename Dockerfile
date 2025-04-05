#
# GunCAD Mirror MSB Dockerfile
#
ARG python=3.13
ARG commit_sha=master
ARG commit_tag=

# STAGE 1: Building lbrynet
# TODO: This is currently congruent with LBRY's build instructions, but we
# could probablay move from ubuntu:24.04 to python:3.8-slim if we wanted to
FROM docker.io/ubuntu:24.04 AS lbrynet
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_ROOT_USER_ACTION=ignore
RUN	apt-get update && \
	apt-get install -y file wget unzip python3-launchpadlib software-properties-common build-essential git libssl-dev && \
	add-apt-repository ppa:deadsnakes/ppa && \
	apt-get update && \
	apt-get install -y python3.8 python3.8-dev python3.8-venv python3-protobuf
# Build LBRY. Note that we have to pull Py3.8(!) because they don't support
# anything newer. Which blows ass. Oh well.
RUN	mkdir /root/buildlbrynet && \
	cd /root/buildlbrynet && \
	git clone https://github.com/lbryio/lbry-sdk && \
	cd lbry-sdk && \
	python3.8 -m venv venv && \
	. venv/bin/activate && \
	make install && \
	ls -alh /root/buildlbrynet/lbry-sdk/venv/bin/lbrynet && \
	file /root/buildlbrynet/lbry-sdk/venv/bin/lbrynet

# STAGE 2: Building the app
FROM docker.io/python:$python-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_ROOT_USER_ACTION=ignore
COPY start.sh /usr/local/bin/start-guncad-mirror
RUN mkdir /app
WORKDIR /app
COPY requirements.txt /app/
RUN	apt-get update && \
	apt-get install -y wget unzip
RUN	pip install --upgrade pip && \
	pip install --no-cache-dir -r requirements.txt
COPY ./ /app/

# STAGE 3: Prod build
FROM docker.io/python:$python-slim AS prod
ARG commit_sha
ARG commit_tag
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV GUNCAD_COMMIT_SHA=$commit_sha
ENV GUNCAD_COMMIT_TAG=$commit_tag
ENV GUNCAD_IN_DOCKER=True
RUN	apt-get update && \
	apt-get install -y curl
RUN	adduser mirror --uid 1000 && \
	mkdir /app && \
	chown -R mirror: /app
COPY --from=builder /usr/local/lib/python3.13/site-packages/ /usr/local/lib/python3.13/site-packages/
COPY --from=lbrynet /opt/lbry-sdk/lbry-venv/bin/lbrynet /usr/local/bin/lbrynet
COPY --from=builder --chown=mirror /app /app
WORKDIR /app
EXPOSE 5567
ENTRYPOINT [ "/bin/bash", "/usr/local/bin/start-guncad-mirror" ]
