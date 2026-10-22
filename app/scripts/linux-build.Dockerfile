# The Linux build image for `smoke_bundle.sh --linux` on a Mac: Ubuntu 22.04
# (the release workflow's linux runner) with Tauri's Linux build dependencies
# (webkit2gtk-4.1 and friends; keep the apt list in step with the linux job in
# .github/workflows/app-release.yml), Xvfb for the headless --version
# check, Node 22 and stable Rust. Built with no context: nothing is copied in,
# the repo is mounted at run time and the caches live in named volumes.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl wget file pkg-config \
      libwebkit2gtk-4.1-dev libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev \
      xvfb xauth \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH
RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
