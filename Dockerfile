FROM python:3.12-slim AS builder

WORKDIR /workspace

# Build deps for webrtcvad (C extension)
RUN apt-get update \
  && apt-get install -y --no-install-recommends gcc python3-dev \
  && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached unless pyproject.toml/setup.py change).
# A minimal src stub satisfies the editable-install metadata step.
COPY pyproject.toml README.md setup.py ./
RUN mkdir -p src/home_agent && touch src/home_agent/__init__.py

RUN python -m pip install --no-cache-dir --upgrade pip "setuptools<81" \
  && python -m pip install --no-cache-dir \
     ".[sonos,camect,caseta,gcal,ui,voice,snmp,net,llm-anthropic,dashboard]" \
  && python -m playwright install --with-deps chromium

# --- Runtime stage (no compiler toolchain) ---
FROM python:3.12-slim

WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Picovoice fingerprints /etc/machine-id — use a fixed value so all containers
# are seen as the same "device" and reuse the cached activation token.
RUN echo '79efcfd5504a43d882acd813ecb70e63' > /etc/machine-id

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Playwright browsers live under ~/.cache (not in site-packages); builder installed chromium there
COPY --from=builder /root/.cache/ms-playwright /root/.cache/ms-playwright

# Shared libs for headless Chromium (builder's --with-deps does not carry into this stage)
RUN apt-get update \
  && python -m playwright install-deps chromium \
  && rm -rf /var/lib/apt/lists/*

# Copy project source + assets (changes here only rebuild from this point)
COPY pyproject.toml README.md setup.py ./
COPY src ./src
COPY models ./models
COPY assets ./assets
COPY db ./db
COPY scripts ./scripts
COPY docs ./docs

# Editable install so the CLI entry point resolves to baked-in source
RUN python -m pip install --no-cache-dir --no-deps -e .

CMD ["home-agent", "--help"]
