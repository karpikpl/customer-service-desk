FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY main.py ./
ENV COPILOT_CLI_EXTRACT_DIR="/opt/copilot-runtime"
RUN uv sync --frozen --no-dev --no-editable && \
    uv run --no-sync python -m copilot download-runtime
ENV PATH="/app/.venv/bin:${PATH}" \
    COPILOT_SKIP_CLI_DOWNLOAD=1 \
    PORT=8088 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1
EXPOSE 8088
CMD ["python", "main.py"]
