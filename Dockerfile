FROM python:3.12-slim

WORKDIR /workspace

# System deps (kept minimal)
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Copy project (so image can run standalone without a volume mount)
COPY pyproject.toml README.md setup.py ./
COPY src ./src
COPY scripts ./scripts
COPY docs ./docs

# Install (include Sonos extra so discovery/playback works)
RUN python -m pip install --upgrade pip \
  && python -m pip install ".[sonos]"

CMD ["home-agent", "run"]

