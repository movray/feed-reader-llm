# Developer tasks. The release itself runs in .github/workflows/release.yml.
IMAGE        ?= feed-reader-llm:dev
PYTHON_IMAGE ?= python:3.13-alpine3.24
TRIVY_IMAGE  ?= aquasec/trivy:0.76.1
CHART        := charts/feed-reader-llm

.PHONY: lock image scan up down lint template

# Regenerate app/requirements.txt (all packages, pinned with hashes) from
# app/requirements.in, with uv in a throwaway container.
lock:
	docker run --rm -u "$$(id -u):$$(id -g)" -e HOME=/tmp -v "$(CURDIR)/app:/w" -w /w $(PYTHON_IMAGE) sh -c '\
	  pip install -q --user uv && /tmp/.local/bin/uv pip compile --quiet --generate-hashes \
	    --python-version 3.13 --python-platform x86_64-unknown-linux-musl \
	    --no-header --custom-compile-command "make lock" requirements.in -o requirements.txt'
	sed -i '1i # Generated from requirements.in with `make lock` - do not edit by hand.' app/requirements.txt

image:
	docker build -t $(IMAGE) .

# Same gate as the release: no HIGH or CRITICAL finding, unfixed ones included.
scan: image
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock $(TRIVY_IMAGE) \
	  image --scanners vuln,secret --exit-code 1 --severity HIGH,CRITICAL $(IMAGE)

# Local test setup: PostgreSQL, migration, generator, API and nginx.
up:
	docker compose up --build -d

down:
	docker compose down

lint:
	helm lint $(CHART)
	helm lint $(CHART) --set postgresql.enabled=true

template:
	helm template feedreader $(CHART) --set postgresql.enabled=true
