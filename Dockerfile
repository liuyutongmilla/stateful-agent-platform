FROM node:22-slim AS console
WORKDIR /console
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /srv/backend
COPY backend/pyproject.toml ./
COPY backend/app ./app
ARG EXTRAS=""
RUN pip install --no-cache-dir ".${EXTRAS}"
COPY --from=console /console/dist /srv/frontend/dist
ENV STATIC_DIR=/srv/frontend/dist CHECKPOINT_DB=/data/checkpoints.sqlite
VOLUME /data
EXPOSE 8000 8001
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
