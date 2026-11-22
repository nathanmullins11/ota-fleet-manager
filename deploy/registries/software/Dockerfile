FROM python:3.12-slim
WORKDIR /app

# Install dependencies first so Docker caches this layer until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
ENV PACKAGES_DIR=/packages
EXPOSE 8080

# Run as an unprivileged user (threat E1): it only reads /packages, mounted read-only.
RUN useradd --system --no-create-home registry
USER registry

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
