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
    && groupadd --gid "$APP_GID" monitor \
    && useradd --create-home --uid "$APP_UID" --gid "$APP_GID" monitor
COPY --chown=monitor:monitor backend /app
COPY --chown=monitor:monitor scripts/container_health.py /app/container_health.py
COPY --chown=monitor:monitor config/update.example.yaml /app/config/update.yaml
USER monitor
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
