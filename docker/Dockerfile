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
RUN apt-get update && for pkg in sqlmap nmap ffuf nuclei jwt-tool httpx-toolkit; do \
        apt-get install -y --no-install-recommends "$pkg" || echo "WARN: $pkg not available"; \
    done && rm -rf /var/lib/apt/lists/*

# Node toolchain + Claude Code CLI for Mode A inside the sandbox.
RUN npm install -g @anthropic-ai/claude-code

# Python deps used by the in-container RPC server and the host-side tools we
# port over (web_search/web_fetch can run in-container too).
RUN python3 -m pip install --break-system-packages --no-cache-dir \
        "fastapi>=0.110" "uvicorn[standard]>=0.27" \
        "httpx>=0.27" "pydantic>=2.6" \
        "requests" "pyjwt" "trafilatura" "ddgs"

# Bake the `zero` package into the image so the in-container tool server
# can `import zero.tools.*` without depending on what the host happens to
# mount at /work. Build context must be the repo root:
#   docker build -t zero-sandbox:latest -f docker/Dockerfile .
COPY src/zero /opt/zero/zero
ENV PYTHONPATH=/opt/zero

COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/tool_server /opt/zero_tool_server
RUN chmod +x /entrypoint.sh

# Sanity check: the entrypoint must be syntactically valid.
RUN bash -n /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
