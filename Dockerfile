FROM python:3.9-slim
WORKDIR /app

# Install system dependencies for building packages
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create directories for data and parameters
RUN mkdir -p /app/ASTER_data /app/params

# Source files will be mounted as volumes, not copied
# Default command (can be overridden in docker-compose)
CMD ["python", "-u", "data_collector.py"]
