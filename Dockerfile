FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/tidalflat.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单 worker + 同步引擎即可满足串行核查作业；多 worker 下写事务由
# BEGIN IMMEDIATE 串行化，SQLite WAL 保证读写不互斥
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "120", "run:app"]
