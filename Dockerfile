FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RIVER_GATEWAY_CONFIG=/app/config.yaml

WORKDIR /app

# PyPI index for the build (pip reads PIP_INDEX_URL); compose passes a mirror if set.
ARG PIP_INDEX_URL=https://pypi.org/simple

# Dependencies get their own layer (installed via a stub package), so a code
# change rebuilds in seconds instead of reinstalling everything.
COPY pyproject.toml README.md ./
RUN mkdir -p src/river_gateway && touch src/river_gateway/__init__.py \
    && pip install . && pip uninstall -y river-gateway \
    && rm -rf src build

COPY src ./src
RUN pip install --no-deps . && rm -rf src build

# Default config; docker-compose.yml mounts your own config.yaml over it.
COPY config.example.yaml /app/config.yaml
RUN useradd --create-home --uid 10001 gateway \
    && mkdir -p /app/data && chown gateway:gateway /app/data
USER gateway

# SQLite database (keys and usage log): keep it on a volume.
VOLUME ["/app/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8080/healthz', timeout=4)"]
CMD ["river-gateway", "serve", "--host", "0.0.0.0", "--port", "8080"]
