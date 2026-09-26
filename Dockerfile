FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    LIVE_CASES_PATH=/app/live_cases.json \
    PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY live_cases.json ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir .

COPY src/ ./repository/src/
COPY tests/ ./repository/tests/
COPY .github/workflows/ ./repository/.github/workflows/
COPY Dockerfile pyproject.toml cloudbuild.reconcile.yaml ./repository/
COPY live_cases.json ./repository/

RUN adduser --disabled-password --gecos "" --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
CMD ["a-share-web"]
