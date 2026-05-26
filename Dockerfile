FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/agentlab

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      cargo \
      git \
      openssh-client \
      rustc \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agentlab

WORKDIR /app
COPY pyproject.toml README.md ./
COPY agentlab ./agentlab

RUN python -m pip install --upgrade pip \
    && python -m pip install .

USER 10001:10001

ENTRYPOINT ["agentlab"]
CMD ["--help"]
