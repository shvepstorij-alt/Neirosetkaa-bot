FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY support_agent/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY support_agent /app/support_agent
CMD ["python", "-m", "support_agent.service"]
