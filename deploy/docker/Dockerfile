# Universal AI Runtime image. Also used for the first-party MCP servers (stdio container sandbox
# and the Kubernetes uar-plugins deployments), with a different command.
#   docker build -f deploy/docker/Dockerfile -t uar-runtime:0.6.0 .
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/runtime:/app/mcp-servers \
    UAR_CONFIG=/config/uar.yaml

WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Source layout is kept so migrations, schemas and fixtures resolve relative to the code.
COPY runtime/uar_runtime ./runtime/uar_runtime
COPY runtime/uarpb ./runtime/uarpb
COPY mcp-servers ./mcp-servers
COPY migrations ./migrations
COPY contracts ./contracts
COPY examples/agents ./examples/agents
COPY examples/fixtures ./examples/fixtures
COPY examples/workspace/docs ./examples/workspace/docs
COPY config/uar.example.yaml ./config/uar.example.yaml

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin uar \
 && mkdir -p /workspace/reports && chown -R 10001:10001 /workspace
USER 10001
EXPOSE 9000 50051
ENTRYPOINT ["python", "-m", "uar_runtime.main"]
CMD ["serve"]
