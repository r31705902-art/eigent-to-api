FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid 10001 --create-home gateway \
    && mkdir -p /data/session && chown -R gateway:gateway /data/session
COPY app ./app
COPY docker-entrypoint.py ./docker-entrypoint.py
EXPOSE 8080
CMD ["python", "docker-entrypoint.py"]
