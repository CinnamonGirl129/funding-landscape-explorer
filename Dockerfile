# Stage 1: download the real QWNTL data and build the local, indexed
# database. This stage's disk usage (raw parquet downloads) never reaches
# the final image, only explorer.duckdb does.
FROM python:3.11-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY build_data.py .
RUN python build_data.py

# Stage 2: the actual runtime image, small and fast to start.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY index.html .
COPY --from=builder /app/explorer.duckdb .

# Render (and most hosts) inject the real port to bind to via $PORT.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
