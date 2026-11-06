# Visual demo helper (demo only). Built with the repository root as the context, because it
# reuses the orchestrator's modules; see demo/visual/helper/compose.yml.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY deploy/orchestrator/requirements.txt orchestrator-requirements.txt
COPY demo/visual/helper/requirements.txt helper-requirements.txt
RUN pip install --no-cache-dir -r orchestrator-requirements.txt -r helper-requirements.txt

COPY deploy/orchestrator/*.py ./
COPY demo/visual/helper/app.py helper_app.py
EXPOSE 8090
CMD ["uvicorn", "helper_app:app", "--host", "0.0.0.0", "--port", "8090", "--log-level", "warning"]
