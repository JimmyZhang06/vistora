# syntax=docker/dockerfile:1.7
ARG PYTHON_BASE=python:3.12.11-slim-bookworm

FROM ${PYTHON_BASE} AS api-wheels
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY apps/api ./apps/api
RUN python -m pip install --require-hashes --only-binary=:all: \
       -r apps/api/requirements-build.txt \
    && python -m pip wheel --require-hashes --only-binary=:all: \
       --wheel-dir /wheels -r apps/api/requirements-prod.txt \
    && python -m pip wheel --no-build-isolation --no-deps \
       --wheel-dir /wheels ./apps/api \
    && install -D -m 0444 apps/api/requirements-prod.txt /locks/api-prod.txt

FROM ${PYTHON_BASE} AS worker-wheels
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY services/worker ./services/worker
RUN python -m pip install --require-hashes --only-binary=:all: \
       -r services/worker/requirements-build.txt \
    && python -m pip wheel --require-hashes --only-binary=:all: \
       --wheel-dir /wheels -r services/worker/requirements-prod.txt \
    && python -m pip wheel --no-build-isolation --no-deps \
       --wheel-dir /wheels ./services/worker \
    && install -D -m 0444 services/worker/requirements-prod.txt /locks/worker-prod.txt

FROM ${PYTHON_BASE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FRAMEFACTORY_ENV=production
RUN groupadd --gid 10001 framefactory && useradd --uid 10001 --gid framefactory \
    --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin framefactory
WORKDIR /opt/framefactory

FROM runtime AS api
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=api-wheels /wheels /wheels
COPY --from=api-wheels /locks/api-prod.txt /locks/api-prod.txt
RUN python -m pip install --no-index --find-links=/wheels --require-hashes \
       -r /locks/api-prod.txt \
    && python -m pip install --no-index --find-links=/wheels --no-deps \
       framefactory-api==0.1.0 \
    && python -m pip check \
    && rm -rf /wheels /locks
COPY --chown=10001:10001 packages/contracts ./packages/contracts
COPY --chown=10001:10001 packages/seeds ./packages/seeds
ENV FRAMEFACTORY_CONTRACT_SCHEMA_DIR=/opt/framefactory/packages/contracts/schemas/v1 \
    FRAMEFACTORY_OFFICIAL_SEED_MANIFEST=/opt/framefactory/packages/seeds/official-skills/v1/manifest.json
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=4 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2).read()"]
CMD ["uvicorn", "framefactory_api.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]

FROM runtime AS migrate
COPY --from=api-wheels /wheels /wheels
COPY --from=api-wheels /locks/api-prod.txt /locks/api-prod.txt
RUN python -m pip install --no-index --find-links=/wheels --require-hashes \
       -r /locks/api-prod.txt \
    && python -m pip install --no-index --find-links=/wheels --no-deps \
       framefactory-api==0.1.0 \
    && python -m pip check \
    && rm -rf /wheels /locks
COPY --chown=10001:10001 db/migrations ./db/migrations
COPY --chown=10001:10001 services/worker/migrations ./services/worker/migrations
USER 10001:10001
CMD ["python", "-m", "framefactory_api.migrate", "--project-root", "/opt/framefactory"]

FROM runtime AS worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=worker-wheels /wheels /wheels
COPY --from=worker-wheels /locks/worker-prod.txt /locks/worker-prod.txt
RUN python -m pip install --no-index --find-links=/wheels --require-hashes \
       -r /locks/worker-prod.txt \
    && python -m pip install --no-index --find-links=/wheels --no-deps \
       framefactory-worker==0.1.0 \
    && python -m pip check \
    && rm -rf /wheels /locks
USER 10001:10001
CMD ["framefactory-worker", "run"]
