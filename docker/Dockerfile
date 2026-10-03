FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/scenario-generator-venv \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md LICENSE LICENSE-3rdparty /app/
COPY scenario_generator /app/scenario_generator

RUN uv sync --locked --no-dev --no-editable \
    && /opt/scenario-generator-venv/bin/python -c "from scenario_generator.config.settings import load_actor_xosc_defaults; load_actor_xosc_defaults('vehicle')"

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    PATH="/opt/scenario-generator-venv/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/scenario-generator-venv /opt/scenario-generator-venv
RUN mkdir -p /tmp/scenario.generator/orthophotos \
    && chmod 1777 /tmp/scenario.generator /tmp/scenario.generator/orthophotos
COPY LICENSE LICENSE-3rdparty /app/
COPY scenario_generator /app/scenario_generator

EXPOSE 8000

CMD ["python", "-m", "scenario_generator.webapp.server"]
