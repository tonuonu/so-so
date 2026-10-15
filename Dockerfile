FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git gcc libc6-dev nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/soso
COPY . .
RUN pip install --no-cache-dir .
RUN useradd --create-home runner
USER runner
WORKDIR /workspace
ENTRYPOINT ["soso"]
