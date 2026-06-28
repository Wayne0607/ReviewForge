# Stage 1: Build frontend
FROM node:20-slim AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts 2>/dev/null || npm install
COPY frontend/ .
RUN npm run build

# Stage 2: Python backend + static files
FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY backend/pyproject.toml backend/
RUN pip install --no-cache-dir -e backend/ 2>/dev/null || \
    pip install --no-cache-dir fastapi uvicorn langchain-openai langchain-core pydantic httpx pyyaml cryptography aiosqlite

# Copy source
COPY backend/src/ backend/src/
COPY skills/ skills/
COPY reviewforge.yaml .

# Copy frontend build output
COPY --from=frontend-build /app/backend/src/reviewforge/static/ backend/src/reviewforge/static/

# Install the package
RUN pip install --no-cache-dir -e backend/

# Create data directory
RUN mkdir -p .reviewforge/events

EXPOSE 8000

CMD ["python", "-m", "reviewforge", "serve", "--host", "0.0.0.0", "--port", "8000"]
