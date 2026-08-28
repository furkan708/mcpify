FROM python:3.12-slim

RUN useradd --create-home --shell /usr/sbin/nologin mcp
WORKDIR /home/mcp
RUN pip install --no-cache-dir mcpify-openapi

USER mcp
ENTRYPOINT ["mcpify"]
