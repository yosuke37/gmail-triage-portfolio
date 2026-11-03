FROM postgres:18
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /backups && chown 10001:10001 /backups
COPY deploy/backup_service.py /backup_service.py
USER 10001:10001
ENTRYPOINT ["python3", "/backup_service.py"]
