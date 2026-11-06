# Visual demo server (demo only). The image holds just the dependencies; the code and the
# page are mounted from the repository at /repo by demo/visual/run.sh.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
EXPOSE 8088
CMD ["uvicorn", "--app-dir", "/repo/demo/visual/server", "app:app", \
     "--host", "0.0.0.0", "--port", "8088", "--log-level", "warning"]
