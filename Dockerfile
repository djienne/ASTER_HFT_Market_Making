# Use the official Ubuntu 22.04 image hosted on Amazon ECR Public
FROM public.ecr.aws/ubuntu/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install prerequisites, add deadsnakes PPA, then install Python 3.9 + build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg software-properties-common build-essential \
  && add-apt-repository -y ppa:deadsnakes/ppa \
  && apt-get update && apt-get install -y --no-install-recommends \
      python3.9 python3.9-venv python3.9-dev \
      gcc g++ \
  && rm -rf /var/lib/apt/lists/*

# Create symlink so 'python' command works
RUN ln -s /usr/bin/python3.9 /usr/bin/python

# Ensure pip is present and up-to-date for Python 3.9
RUN python3.9 -m ensurepip --upgrade \
 && python3.9 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy and install Python dependencies
COPY requirements.txt .
RUN python3.9 -m pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./
COPY .env ./

# Create runtime directories
RUN mkdir -p /app/ASTER_data /app/params

# Default command (can be overridden in docker-compose)
CMD ["python3.9", "-u", "data_collector.py"]

