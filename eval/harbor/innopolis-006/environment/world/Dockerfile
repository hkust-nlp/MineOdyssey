FROM localhost/anonymous-navigation:linux-cpu
ADD source.tar.gz /workspace/mcbots/
COPY assets/ /inputs/
COPY gateway.py task-spec.json /opt/harbor-nav/
WORKDIR /workspace/mcbots
ARG NAVIGATION_MAP_ID=innopolis
ARG NAVIGATION_MAP_ASSET=navigation-1.21.11-innopolis.zip
ENV PYTHONDONTWRITEBYTECODE=1 RENDER_MODE=cpu MCBOTS_NAV_REMOTE_BASH_NETWORK_DISABLED=true
RUN test -f /inputs/snapshot.tar.gz || test -f "/inputs/${NAVIGATION_MAP_ASSET}" || (echo 'Supply the pinned map asset before building.' >&2; exit 1)
RUN if test -f /inputs/runtime.tar.gz; then tar -xzf /inputs/runtime.tar.gz -C /workspace/mcbots; else uv run --frozen python scripts/eval/prepare-navigation-runtime.py --skip-smoke; fi
RUN if test -f /inputs/snapshot.tar.gz; then tar -xzf /inputs/snapshot.tar.gz -C /workspace/mcbots; else uv run --frozen python scripts/snapshot/prepare-navigation-snapshot.py --map "$NAVIGATION_MAP_ID" --downloads-dir /inputs; fi
CMD ["python3", "/opt/harbor-nav/gateway.py"]
