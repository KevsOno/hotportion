FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy only the backend requirements file
COPY backend/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy ONLY the backend application code (not frontend)
COPY backend/main.py .
COPY backend/migrations/ ./migrations/

# (Optional) If you have other backend modules, copy them too
# COPY backend/app/ ./app/

# Create non-root user
RUN useradd -m -u 1000 hotportion && chown -R hotportion:hotportion /app
USER hotportion

# Expose port
EXPOSE 8000

# Health check (fly.io uses this)
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Run with 1 worker (fly.io scales horizontally)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
