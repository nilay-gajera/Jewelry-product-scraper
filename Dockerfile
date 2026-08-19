FROM node:22-slim AS admin-build

WORKDIR /app/admin

COPY admin/package.json admin/package-lock.json ./
RUN npm ci
COPY admin/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

WORKDIR /app

COPY . .
COPY --from=admin-build /app/admin_dist ./admin_dist
RUN python -m pip install --root-user-action=ignore --upgrade pip \
    && python -m pip install --root-user-action=ignore .

EXPOSE 10000

CMD ["sh", "-c", "uvicorn service:app --host 0.0.0.0 --port ${PORT:-10000}"]
