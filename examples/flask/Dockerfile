# syntax=docker/dockerfile:1
FROM python:3.11-slim AS publisher
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir appguard-runtime==0.0.1

FROM publisher AS protected-build
# Build with --no-cache-filter protected-build: secret changes do not invalidate cache.
RUN --mount=type=bind,from=application,target=/source \
    --mount=type=secret,id=issuer_key,required=true \
    --mount=type=secret,id=publisher_public,required=true \
    --mount=type=secret,id=code_key,required=true \
    appguard build \
      --source /source --config /source/guard.toml \
      --issuer-key /run/secrets/issuer_key \
      --code-key /run/secrets/code_key --out /release \
    && appguard build-runtime \
      --public-key /run/secrets/publisher_public \
      --code-key /run/secrets/code_key --out /wheels

FROM python:3.11-slim AS dependencies
COPY --from=protected-build /wheels /wheels
COPY --from=application requirements.txt /requirements.txt
RUN python -m pip install --no-cache-dir --prefix=/install /wheels/*.whl -r /requirements.txt

FROM python:3.11-slim
COPY --from=dependencies /install /usr/local
WORKDIR /app
COPY --from=protected-build /release/bundle/tree/ /app/
COPY --from=protected-build /release/bundle/manifest.json /opt/appguard/bundle/manifest.json
COPY --from=protected-build /release/bundle/modules/ /opt/appguard/bundle/modules/
RUN useradd --uid 10001 --create-home appguard \
    && mkdir -p /var/lib/appguard \
    && chown 10001:10001 /var/lib/appguard
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "example_web.web_app:app"]
