FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg && rm /var/lib/apt/lists/* -f
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY config ./config
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["poop-detector"]
CMD ["run"]
