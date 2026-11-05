FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --uid 10001 --create-home gateway && mkdir /state && chown gateway /state
USER gateway
ENTRYPOINT ["gateway"]
CMD ["serve", "--host", "0.0.0.0", "--state", "/state", "--orthanc-host", "orthanc"]
