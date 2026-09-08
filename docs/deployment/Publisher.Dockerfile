# Separate trusted Publisher image: GitHub authentication never enters app/runner images.
FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git gh \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 contribos \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent contribos
COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir .
USER 10001:10001
ENTRYPOINT ["contribos", "publisher-worker"]
