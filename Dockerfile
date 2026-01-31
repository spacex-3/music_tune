FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./
COPY .env.example .env.example

# Create directories for persistent data
RUN mkdir -p /app/cache /app/data

# Environment variables (can be overridden at runtime)
ENV TUNEHUB_API_KEY=""
ENV TUNEHUB_API_SECRET=""
ENV SERVER_HOST="0.0.0.0"
ENV SERVER_PORT="4040"
ENV SUBSONIC_USER="admin"
ENV SUBSONIC_PASSWORD="admin"
ENV DEFAULT_QUALITY="flac"

# Expose port
EXPOSE 4040

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:4040/rest/ping.view?u=admin&p=admin&v=1.16.0&c=healthcheck || exit 1

# Volume for persistent data (cache, logs, credits log)
VOLUME ["/app/cache", "/app/data"]

# Run the server
CMD ["python", "server.py"]
