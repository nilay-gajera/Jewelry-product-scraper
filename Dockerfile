FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

WORKDIR /app

COPY . .
RUN python -m pip install --upgrade pip \
    && python -m pip install .

EXPOSE 10000

CMD ["sh", "-c", "uvicorn service:app --host 0.0.0.0 --port ${PORT:-10000}"]
