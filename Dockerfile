FROM python:3.12-slim

WORKDIR /app

# Copy the echo probe script
COPY echo_probe.py /app/echo_probe.py

# Optional: create non-root user
RUN useradd -m appuser && chown -R appuser /app
USER appuser

ENTRYPOINT ["python", "/app/echo_probe.py"]
