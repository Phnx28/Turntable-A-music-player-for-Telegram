FROM python:3.13-slim

RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app
COPY --chown=app:app pyproject.toml ./
RUN pip install --no-cache-dir .
COPY --chown=app:app app.py core.py external.py telegram_service.py ./
COPY --chown=app:app static ./static

RUN mkdir -p /data && chown app:app /data
ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000
USER app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
