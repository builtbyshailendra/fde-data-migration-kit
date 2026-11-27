# syntax=docker/dockerfile:1

# --- build: produce a wheel and install it with its dependencies into a venv ---
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip wheel --no-deps --wheel-dir /wheels . \
 && /opt/venv/bin/pip install "$(ls /wheels/*.whl)[postgres]"

# --- runtime: the venv only, running as an unprivileged user ---
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="fde-data-migration-kit" \
      org.opencontainers.image.description="Profile, map, validate, deduplicate, load and reconcile legacy data" \
      org.opencontainers.image.licenses="MIT"
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --system --gid 10001 migkit \
 && useradd --system --uid 10001 --gid migkit --home-dir /work --create-home migkit
COPY --from=build /opt/venv /opt/venv
COPY examples /opt/migkit/examples
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["migkit"]
CMD ["--help"]
