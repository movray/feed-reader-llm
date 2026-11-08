# syntax=docker/dockerfile:1
#
# Image of feed-reader-llm: generator, API and schema migration in one image,
# the role is picked by the first argument (see app/entrypoint.sh).
#
# The runtime stage has no pip: the dependencies come as a finished venv
# from the build stage, so neither pip nor the packages vendored into it
# end up in the image (Trivy finds known CVEs in those).

ARG PYTHON_IMAGE=python:3.13-alpine3.24@sha256:1e1f6334529d1fb26a2ef619d7cffb30a7c7d7cc1cc7338ebee283a58d1aeaca

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY app/requirements.txt /tmp/requirements.txt
# --require-hashes: only the reviewed lockfile versions (app/requirements.txt).
# --only-binary: every dependency has a musllinux wheel, nothing is compiled.
RUN python -m venv --without-pip /venv \
 && python -m pip --python /venv/bin/python install \
      --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements.txt

FROM ${PYTHON_IMAGE}
ARG VERSION=dev
LABEL org.opencontainers.image.title="feed-reader-llm" \
      org.opencontainers.image.description="RSS feed aggregator that summarizes articles with a local LLM" \
      org.opencontainers.image.source="https://github.com/movray/feed-reader-llm" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}"

RUN apk upgrade --no-cache \
 && python -m pip uninstall -y pip \
 && rm -rf /root/.cache \
 && adduser -D -H -u 10001 -s /sbin/nologin feedreader \
 && mkdir -p /html && chown 10001:10001 /html

COPY --from=build /venv /venv
COPY --chmod=0755 app/entrypoint.sh /app/entrypoint.sh
COPY app/*.py app/schema.sql /app/

ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTML_DIR=/html \
    APP_VERSION=${VERSION}

USER 10001:10001
WORKDIR /app
EXPOSE 5000
VOLUME ["/html"]
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["api"]
