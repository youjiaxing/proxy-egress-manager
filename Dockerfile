FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data CORE_CONFIG_DIR=/core
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 10001 pem && useradd -u 10001 -g pem -M pem && mkdir -p /data /core && chown -R pem:pem /data /core
COPY gateway ./gateway
COPY scheduler ./scheduler
COPY web ./web
COPY scripts/maintenance.py ./scripts/maintenance.py
COPY main.py LICENSE ./
COPY docs/third-party.md ./THIRD-PARTY.md
USER 10001:10001
VOLUME ["/data", "/core"]
EXPOSE 8080 1081 1082
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 CMD ["python", "main.py", "healthcheck"]
ENTRYPOINT ["python", "main.py"]
CMD ["serve"]
