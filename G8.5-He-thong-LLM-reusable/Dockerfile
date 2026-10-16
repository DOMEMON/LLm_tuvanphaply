FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY g85/tabular_lab/requirements.txt ./g85/tabular_lab/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /app/.runtime/g85-tabular-service/datasets \
    && chown -R app:app /app/.runtime
COPY g85 ./g85
USER app
EXPOSE 8086
CMD ["python", "-m", "uvicorn", "g85.tabular_lab.service:app", "--host", "0.0.0.0", "--port", "8086", "--workers", "1", "--no-access-log"]
