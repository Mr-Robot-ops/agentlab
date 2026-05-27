FROM rust:1-slim-bookworm AS rust-toolchain

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/agentlab \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/home/agentlab/.cargo

COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
COPY --from=rust-toolchain /usr/local/cargo/bin /usr/local/cargo/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      git \
      openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN /usr/sbin/useradd --create-home --uid 10001 --shell /usr/sbin/nologin agentlab \
    && mkdir -p /home/agentlab/.cargo \
    && chown -R agentlab:agentlab /home/agentlab/.cargo

ENV PATH=/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin

RUN echo "$PATH" \
    && command -v cargo \
    && cargo --version \
    && command -v rustc \
    && rustc --version

WORKDIR /app
COPY pyproject.toml README.md ./
COPY agentlab ./agentlab

RUN python -m pip install --upgrade pip \
    && python -m pip install .

USER 10001:10001

ENTRYPOINT ["agentlab"]
CMD ["--help"]
