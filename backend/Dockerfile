FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ROOT=/app \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
ARG APP_UID=10001
ARG APP_GID=10001
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check -r /app/requirements.txt \
    && if ! getent group "$APP_GID" >/dev/null; then groupadd --gid "$APP_GID" monitor; fi \
    && if ! getent passwd "$APP_UID" >/dev/null; then useradd --create-home --uid "$APP_UID" --gid "$APP_GID" monitor; fi \
    && mkdir -p /app/data/update \
    && chown -R "$APP_UID:$APP_GID" /app
COPY --chown=${APP_UID}:${APP_GID} backend /app
COPY --chown=${APP_UID}:${APP_GID} scripts/container_health.py /app/container_health.py
COPY --chown=${APP_UID}:${APP_GID} config/update.example.yaml /app/config/update.yaml
USER ${APP_UID}:${APP_GID}
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
