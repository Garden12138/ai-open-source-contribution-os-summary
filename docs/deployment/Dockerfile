FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m pip install --no-cache-dir . \
    && groupadd --gid 10001 contribos \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent contribos \
    && mkdir -p /data/artifacts \
    && chown -R contribos:contribos /data

USER 10001:10001

VOLUME ["/data"]

ENTRYPOINT ["contribos"]
