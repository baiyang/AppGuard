# syntax=docker/dockerfile:1
FROM python:3.11-slim AS runtime-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml setup.py appguard_host.py appguard_flask.py ./
COPY runtime/guard_runtime.pyx runtime/guard_runtime.pyx
# Build secrets do not invalidate Docker's cache; the manifest changes per release.
COPY --from=release manifest.json /release-manifest.json
RUN --mount=type=secret,id=publisher_public,required=true \
    --mount=type=secret,id=bootstrap_key,required=true \
    APPGUARD_PUBLIC_KEY="$(cat /run/secrets/publisher_public)" \
    APPGUARD_BOOTSTRAP_KEY="$(cat /run/secrets/bootstrap_key)" \
    python -m pip wheel . --no-deps --wheel-dir /wheels

FROM python:3.11-slim AS dependencies
COPY --from=runtime-build /wheels /wheels
COPY --from=application requirements.txt /requirements.txt
RUN python -m pip install --no-cache-dir --prefix=/install /wheels/*.whl -r /requirements.txt

FROM python:3.11-slim
COPY --from=dependencies /install /usr/local
WORKDIR /app
COPY --from=release tree/ /app/
COPY --from=release manifest.json /opt/appguard/bundle/manifest.json
COPY --from=release modules/ /opt/appguard/bundle/modules/
COPY --from=release functions/ /opt/appguard/bundle/functions/
RUN useradd --uid 10001 --create-home appguard \
    && mkdir -p /var/lib/appguard \
    && chown 10001:10001 /var/lib/appguard
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "web_app:app"]
