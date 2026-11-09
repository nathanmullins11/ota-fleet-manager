# Small image with the release tool's dependencies, so the demo needs nothing installed
# on the host besides Docker. Built with tools/ as the context:
#   docker build -t ota-demo-tools -f demo/lib/tools.Dockerfile tools
FROM python:3.12-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
