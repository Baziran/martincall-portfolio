FROM python:3.14.6-slim-bookworm AS martincall-app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.lock ./

RUN python -m pip install --requirement requirements.lock

COPY scripts/write_build_info.py ./scripts/write_build_info.py
COPY src ./src

ARG BUILD_GIT_SHA
ARG BUILD_DATE=
ARG BUILD_VERSION=

RUN test -n "${BUILD_GIT_SHA}" && test "${BUILD_GIT_SHA}" != "unknown" \
    && BUILD_GIT_SHA="${BUILD_GIT_SHA}" BUILD_DATE="${BUILD_DATE}" BUILD_VERSION="${BUILD_VERSION}" \
    python scripts/write_build_info.py

RUN python -m pip install -e . --no-deps --no-build-isolation

COPY scripts ./scripts

LABEL org.opencontainers.image.revision="${BUILD_GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "aef_terminal.ui.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
