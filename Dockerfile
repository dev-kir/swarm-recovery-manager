# Dockerfile for Recovery Manager
# Compatible with ARM64 (Raspberry Pi) and AMD64

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY recovery_manager.py .

# Create log directory and file
RUN mkdir -p /var/log && \
    touch /var/log/recovery-manager.log && \
    chmod 666 /var/log/recovery-manager.log

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Default configuration (can be overridden at runtime)
ENV PYMONNET_URL=http://pymonnet-server:6969
ENV POLL_INTERVAL=5
ENV LOG_LEVEL=INFO

# Run the application
CMD ["python", "-u", "recovery_manager.py"]
