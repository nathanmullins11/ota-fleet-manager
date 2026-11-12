FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install dependencies first so Docker caches this layer until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py release-public.pem ./
# The signed firmware every device starts on (kept identical to the registry's copy by
# tools/release.py and checked by a test). Installed into ~/firmware on first start.
COPY factory/ factory/

# Run as an unprivileged user. Installed firmware lives in its home folder, which survives
# container restarts (not removal).
RUN useradd --create-home agent
USER agent

CMD ["python", "agent.py"]
