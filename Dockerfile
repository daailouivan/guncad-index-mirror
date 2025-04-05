#
# GunCAD Index MSB Dockerfile
#
ARG python=3.13
ARG commit_sha=master
ARG commit_tag=

# STAGE 1: Building deps up
FROM docker.io/python:$python-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_ROOT_USER_ACTION=ignore
COPY start.sh /usr/local/bin/start-guncad-mirror
RUN mkdir /app
WORKDIR /app
COPY requirements.txt /app/
RUN	apt-get update && \
	apt-get install -y wget unzip && \
	wget https://github.com/lbryio/lbry-sdk/releases/download/v0.113.0/lbrynet-linux.zip && \
	unzip lbrynet-linux.zip -d /usr/local/bin/
RUN	pip install --upgrade pip && \
	pip install --no-cache-dir -r requirements.txt
COPY ./ /app/

# STAGE 2: Prod build
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
COPY --from=builder /usr/local/bin/ /usr/local/bin/
COPY --from=builder --chown=mirror /app /app
WORKDIR /app
EXPOSE 5567
ENTRYPOINT [ "/bin/bash", "/usr/local/bin/start-guncad-mirror" ]
