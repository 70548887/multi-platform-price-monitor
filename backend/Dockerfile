FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check -r /app/requirements.txt \
    && useradd --create-home --uid 10001 monitor
COPY --chown=monitor:monitor backend /app
COPY --chown=monitor:monitor scripts/container_health.py /app/container_health.py
USER monitor
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
