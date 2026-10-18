# Public demo image. It must NEVER contain Candor's code, weights or credentials (deploy_webapp.sh checks).
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PUBLIC_MODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py demo_core.py candor_tools.py system_prompt.py ./
COPY static ./static
RUN useradd -m appuser && chown -R appuser /app
USER appuser
# One worker: sessions are held in memory (the service is also pinned to one instance).
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
