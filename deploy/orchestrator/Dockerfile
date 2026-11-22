FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install dependencies first so Docker caches this layer until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

# Run as an unprivileged user (threat E1). The service writes nothing to disk; it reads the
# broker's CA certificate, mounted read-only.
RUN useradd --system --no-create-home orchestrator
USER orchestrator

CMD ["python", "main.py"]
