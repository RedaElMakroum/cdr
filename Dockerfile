FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements files
COPY requirements.txt requirements-api.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt -r requirements-api.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 5001

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=api.py
ENV HEMS_WORK_DIR=/app

# Run API server
CMD ["python", "api.py"]
