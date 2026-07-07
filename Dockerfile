FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/iKeilo/LdxpM" \
      org.opencontainers.image.description="LdxpM shop stock monitor"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8765 \
    DB_PATH=/app/data/ldxp_stock_webapp.sqlite3

WORKDIR /app

COPY app.py /app/app.py

RUN mkdir -p /app/data

EXPOSE 8765

CMD ["python", "/app/app.py"]
