FROM node:22-alpine AS frontend-build

WORKDIR /app/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    FRONTEND_DIST=/app/frontend/dist \
    PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir .

COPY --from=frontend-build /app/frontend/dist ./frontend/dist
COPY src/ ./repository/src/
COPY frontend/src/ ./repository/frontend/src/
COPY tests/ ./repository/tests/
COPY .github/workflows/ ./repository/.github/workflows/
COPY Dockerfile pyproject.toml cloudbuild.reconcile.yaml ./repository/

RUN adduser --disabled-password --gecos "" --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
CMD ["a-share-web"]
