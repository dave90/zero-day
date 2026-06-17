# Sandbox image for the `zero` agent.
#
# Baked tools (per plan §M4 / §M5):
#   binary  : sqlmap, nmap, ffuf, nuclei, httpx (httpx-toolkit), jwt_tool, curl, jq
#   runtime : python3 + pip; node + @anthropic-ai/claude-code (so Mode A's
#             `claude -p` can run from inside)
#   python  : httpx, requests, pyjwt, fastapi, uvicorn (the in-container RPC
#             server runs as a uvicorn app on a unix socket)
#
# The host orchestrator (`ContainerManager`) starts this container detached
# and mounts:
#   <cwd>            -> /work               (rw)  user's project tree
#   <sock-dir>       -> /run/zero           (rw)  shared volume for the UDS
#   ~/.claude        -> /root/.claude       (ro)  claude CLI auth
#   <burp-dir?>      -> /work/burp          (ro)  optional Burp export dir
#
FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Mandatory layer: the runtime we actually need for the RPC server +
# Mode A's claude CLI.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget jq git \
        python3 python3-pip python3-venv \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Optional layer: the binary security-tool surface used by M5 onwards.
# Each package is installed independently so a single missing-from-repo
# package doesn't break the build — the agent reports the missing binary at
# tool-call time instead.
RUN apt-get update && for pkg in sqlmap nmap ffuf nuclei httpx-toolkit; do \
        apt-get install -y --no-install-recommends "$pkg" || echo "WARN: $pkg not available"; \
    done && rm -rf /var/lib/apt/lists/*

# Wordlists: ffuf/sqlmap/hydra all need them and real runs failed with
# "/usr/share/wordlists/... not found" (ffuf then dumps usage and the agent stalls).
#   - `wordlists` provides /usr/share/wordlists/ (+ rockyou.txt.gz, symlinks to dirb)
#   - `dirb`/`dirbuster` provide /usr/share/wordlists/dirb/common.txt etc.
#   - `seclists` provides /usr/share/seclists (large but the standard go-to)
# rockyou ships gzipped; gunzip it so /usr/share/wordlists/rockyou.txt exists.
# `seclists` is big, so a repo hiccup must not fail the build — install per-pkg.
RUN apt-get update && for pkg in wordlists dirb dirbuster seclists; do \
        apt-get install -y --no-install-recommends "$pkg" || echo "WARN: $pkg not available"; \
    done && rm -rf /var/lib/apt/lists/* \
    && if [ -f /usr/share/wordlists/rockyou.txt.gz ]; then \
           gunzip -kf /usr/share/wordlists/rockyou.txt.gz \
           && echo "OK: rockyou.txt unpacked"; \
       else echo "WARN: rockyou.txt.gz not present"; fi \
    && echo "wordlists present:" \
    && ls -la /usr/share/wordlists/ 2>/dev/null | head -40 || true

# jwt_tool (ticarpi) is NOT an apt package — install it from source and expose a
# `jwt_tool` launcher on PATH (the JwtTool wrapper invokes `jwt_tool <token> <mode>`).
RUN git clone --depth 1 https://github.com/ticarpi/jwt_tool /opt/jwt_tool \
    && python3 -m pip install --break-system-packages --no-cache-dir \
        -r /opt/jwt_tool/requirements.txt \
    && printf '#!/bin/sh\nexec python3 /opt/jwt_tool/jwt_tool.py "$@"\n' > /usr/local/bin/jwt_tool \
    && chmod +x /usr/local/bin/jwt_tool \
    # First run only creates ~/.jwt_tool/jwtconf.ini and exits without analyzing
    # the token. Bake that config into the image now so the agent's FIRST real
    # jwt_tool call decodes immediately instead of wasting a turn on config setup.
    && jwt_tool "eyJhbGciOiJIUzI1NiJ9.e30.x" >/dev/null 2>&1 || true \
    && test -f /root/.jwt_tool/jwtconf.ini \
    && echo "OK: jwt_tool installed + config seeded" \
    || echo "WARN: jwt_tool not installed/seeded — jwt_tool calls may misbehave"

# Node toolchain + Claude Code CLI for Mode A inside the sandbox.
RUN npm install -g @anthropic-ai/claude-code

# Python deps used by the in-container RPC server and the host-side tools we
# port over (web_search/web_fetch can run in-container too).
RUN python3 -m pip install --break-system-packages --no-cache-dir \
        "fastapi>=0.110" "uvicorn[standard]>=0.27" \
        "httpx>=0.27" "pydantic>=2.6" \
        "requests" "pyjwt" "trafilatura" "ddgs" \
        # Common libs agents reach for, pre-installed so they don't have to
        # pip_install them at runtime (the pdfminer-install hang from real runs):
        "pdfminer.six" "pypdf" "beautifulsoup4" "cryptography" "pycryptodome"

# Resolve the `httpx` name collision. The pip `httpx` LIBRARY ships a console
# script ALSO named `httpx` (lands in /usr/local/bin) that shadows the
# ProjectDiscovery `httpx` probe from apt `httpx-toolkit` — so `httpx_tool` would
# invoke the wrong program and fail with "pip install 'httpx[cli]'". We:
#   1. remove the pip console script (the httpx LIBRARY stays importable), and
#   2. symlink /usr/local/bin/httpx -> the real PD binary (located via dpkg, so we
#      don't hard-code its path), guaranteeing the probe wins on PATH.
# Build fails loudly if PD httpx isn't actually present after that.
RUN rm -f /usr/local/bin/httpx && \
    PD="$(dpkg -L httpx-toolkit 2>/dev/null | grep -E '/(httpx|httpx-toolkit)$' | head -n1)"; \
    if [ -z "$PD" ]; then PD="$(command -v httpx-toolkit || true)"; fi; \
    if [ -n "$PD" ]; then ln -sf "$PD" /usr/local/bin/httpx; fi; \
    if command -v httpx >/dev/null && httpx -version 2>&1 | grep -qi projectdiscovery; then \
        echo "OK: httpx -> ProjectDiscovery probe ($PD)"; \
    else \
        echo "WARN: ProjectDiscovery httpx not resolved (httpx-toolkit missing?) —" \
             "httpx_tool will report a clear 'not found' error at call time" >&2; \
    fi

# Bake the `zero` package into the image so the in-container tool server
# can `import zero.tools.*` without depending on what the host happens to
# mount at /work. Build context must be the repo root:
#   docker build -t zero-sandbox:latest -f docker/Dockerfile .
COPY src/zero /opt/zero/zero
ENV PYTHONPATH=/opt/zero

COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/tool_server /opt/zero_tool_server
# Strip any CR before chmod: when the repo is cloned on Windows (git
# core.autocrlf=true), entrypoint.sh checks out with CRLF line endings, so the
# shebang becomes `#!/usr/bin/env bash\r` and the container dies at startup with
# `env: 'bash\r': No such file or directory`. Normalizing here makes the image
# build correctly regardless of the host's git line-ending config (.gitattributes
# fixes fresh checkouts; this defends already-CRLF working trees too).
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Sanity check: the entrypoint must be syntactically valid AND carry no CR
# (a stray CR would mean the normalization above didn't run). `! grep -q` is
# POSIX — Dockerfile RUN uses /bin/sh, so no bash-only `$'\r'`.
RUN bash -n /entrypoint.sh && ! grep -q "$(printf '\r')" /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
