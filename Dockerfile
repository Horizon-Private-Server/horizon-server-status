FROM python:3.12-slim

RUN pip install aiohttp

WORKDIR /app

# Copy the echo probe script
COPY run.py /app/run.py

# Optional: create non-root user
RUN useradd -m appuser && chown -R appuser /app
USER appuser

ENTRYPOINT ["python", "/app/run.py"]
