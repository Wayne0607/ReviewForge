# Stage 1: Build frontend
FROM node:20-slim AS frontend-build
# WORKDIR is /app/frontend so Vite's outDir '../backend/src/reviewforge/static'
# resolves to /app/backend/src/reviewforge/static (matched by the stage-2 COPY).
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# Stage 2: Python backend + static files
FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY backend/pyproject.toml backend/
RUN pip install --no-cache-dir -e backend/

# Copy source
COPY backend/src/ backend/src/
COPY skills/ skills/
COPY reviewforge.yaml .
COPY .env.example .

# Copy frontend build output
COPY --from=frontend-build /app/backend/src/reviewforge/static/ backend/src/reviewforge/static/

# Install the package
RUN pip install --no-cache-dir -e backend/

# Create data directory
RUN mkdir -p .reviewforge/events

# E3: 非 root 用户
RUN useradd -m app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["python", "-m", "reviewforge", "serve", "--host", "0.0.0.0", "--port", "8000"]
