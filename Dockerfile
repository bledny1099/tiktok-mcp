FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .

COPY tiktok_mcp.py server.py download_model.py ./

RUN useradd -m -u 1000 appuser && \
    mkdir -p /var/lib/tiktok-mcp && \
    chown -R appuser:appuser /app /var/lib/tiktok-mcp

USER appuser

ENV TIKTOK_MCP_TRANSPORT=http \
    TIKTOK_MCP_HOST=0.0.0.0 \
    TIKTOK_MCP_PORT=8770 \
    TIKTOK_MCP_WORK_DIR=/var/lib/tiktok-mcp

EXPOSE 8770

CMD ["python", "server.py"]
