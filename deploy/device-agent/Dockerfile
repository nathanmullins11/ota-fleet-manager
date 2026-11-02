FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install dependencies first so Docker caches this layer until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

# Run as an unprivileged user.
RUN useradd --create-home agent
USER agent

CMD ["python", "agent.py"]