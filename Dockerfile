FROM amazonlinux:2023

RUN dnf update -y && \
    dnf install -y python3.11 python3.11-pip shadow-utils tzdata && \
    dnf clean all && \
    rm -rf /var/cache/dnf

ENV PYTHON=/usr/bin/python3.11

# Same fixed uid/gid convention as chat_agent's/ivr_agent's image.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -r -g ${APP_GID} bksystems && \
    useradd -r -u ${APP_UID} -g bksystems -d /app -s /sbin/nologin bksystems

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN ${PYTHON} -m pip install --no-cache-dir --upgrade pip && \
    ${PYTHON} -m pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=bksystems:bksystems app/    /app/app/

# 8030 - the next agent port after chat_agent's 8010 and ivr_agent's 8020.
# Published so smartgateway's IrnsAgentClient can reach it; not meant to be
# internet-reachable otherwise. Unlike chat-agent/ivr-agent, this port is
# never proxied through AgentRouteFilter - smartgateway calls it directly,
# it is never reached from a browser.
EXPOSE 8030

USER bksystems

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ${PYTHON} -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8030/health', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/bin/python3.11", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8030"]
