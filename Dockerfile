#
# GunCAD Mirror MSB Dockerfile
#
ARG python=3.13
ARG lbrynet=v0.113.0
ARG commit_sha=master
ARG commit_tag=

# STAGE 1: Building lbrynet
FROM docker.io/ubuntu:24.04 AS lbrynet
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_ROOT_USER_ACTION=ignore
RUN	apt-get update && \
	apt-get install -y wget file unzip python3-launchpadlib software-properties-common build-essential git libssl-dev && \
	add-apt-repository ppa:deadsnakes/ppa && \
	apt-get update && \
	apt-get install -y python3.8 python3.8-dev python3.8-venv python3-protobuf
# Build LBRY. Note that we have to pull Py3.8(!) because they don't support
# anything newer. Which blows ass. Oh well.
RUN	mkdir /root/buildlbrynet && \
	cd /root/buildlbrynet && \
	git clone https://github.com/lbryio/lbry-sdk && \
	git checkout $lbrynet && \
	cd lbry-sdk && \
	python3.8 -m venv venv && \
	. venv/bin/activate && \
	make install && \
	pip3 install pyinstaller && \
	pyinstaller --onefile --name lbrynet lbry/extras/cli.py && \
	./dist/lbrynet --version

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
COPY --from=builder /usr/local/bin/start-guncad-mirror /usr/local/bin/start-guncad-mirror
COPY --from=lbrynet /root/buildlbrynet/lbry-sdk/dist/lbrynet /usr/local/bin/lbrynet
COPY --from=builder --chown=mirror /app /app
WORKDIR /app
EXPOSE 5567
ENTRYPOINT [ "/bin/bash", "/usr/local/bin/start-guncad-mirror" ]
