FROM python:3.12-slim

RUN useradd --create-home --shell /usr/sbin/nologin mcp
WORKDIR /home/mcp
COPY examples/petstore.json /app/examples/petstore.json
RUN pip install --no-cache-dir mcpify-openapi

USER mcp
ENTRYPOINT ["mcpify"]
# Default: a live stdio MCP server over the bundled petstore example.
# Your own API: docker run -i ghcr.io/furkan708/mcpify:latest serve ./openapi.json --read-only
CMD ["serve", "/app/examples/petstore.json", "--read-only"]
