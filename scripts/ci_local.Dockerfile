# The Linux leg of scripts/ci_local.sh: what ubuntu-latest gives the GitHub
# workflow (git, tmux, uv), as an unprivileged user like a runner's.
FROM python:3.12-slim
RUN apt-get update -q && apt-get install -yq --no-install-recommends git tmux procps ca-certificates \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv \
    && useradd --create-home ci
USER ci
# Created as the ci user, so a new cache volume mounted here is the ci user's too.
RUN mkdir -p /home/ci/.cache/uv
ENV UV_PYTHON_PREFERENCE=managed UV_LINK_MODE=copy
RUN git config --global user.name ci && git config --global user.email ci@localhost \
    && git config --global init.defaultBranch main
