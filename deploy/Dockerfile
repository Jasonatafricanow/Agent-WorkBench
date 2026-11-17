# WorkBench API + timers. Single writer to a SQLite file on a LOCAL volume (not SMB/NFS).
FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY db ./db
RUN pip install --no-cache-dir -e . tzdata
ENV WORKBENCH_DB=/data/workbench.sqlite PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8790
CMD ["python", "-m", "workdesk", "serve", "--host", "0.0.0.0", "--port", "8790"]
