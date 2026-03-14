FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY packages/ ./packages/
COPY configs/ ./configs/
COPY agent.json agent_metadata.json ./
COPY frontend/ ./frontend/

# Expose Olas standard port and existing app port
EXPOSE 8716
EXPOSE 8717

# Olas health check on standard port
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8716/healthcheck')" || exit 1

# Run on Olas standard port 8716
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8716"]
