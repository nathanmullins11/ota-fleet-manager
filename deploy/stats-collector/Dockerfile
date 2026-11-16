FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py .
# Runs as root: the Docker socket it reads is owned by root on the host.
CMD ["python", "collector.py"]
