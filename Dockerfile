FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY backend/pyproject.toml backend/
RUN pip install --no-cache-dir -e backend/ 2>/dev/null || \
    pip install --no-cache-dir fastapi uvicorn langchain-openai langchain-core pydantic httpx pyyaml cryptography

# Copy source
COPY backend/src/ backend/src/
COPY skills/ skills/
COPY reviewforge.yaml .
COPY .env.example .

# Install the package
RUN pip install --no-cache-dir -e backend/

# Create data directory
RUN mkdir -p .reviewforge/events

EXPOSE 8000

CMD ["python", "-m", "reviewforge", "serve", "--host", "0.0.0.0", "--port", "8000"]
