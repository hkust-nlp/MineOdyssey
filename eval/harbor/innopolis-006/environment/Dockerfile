FROM localhost/anonymous-navigation:linux-cpu
ADD agent-source.tar.gz /opt/navigation-agent/
COPY nav.py /usr/local/bin/nav
RUN chmod 755 /usr/local/bin/nav
ENV PYTHONPATH=/opt/navigation-agent PYTHONDONTWRITEBYTECODE=1
RUN /opt/mcbots-venv/bin/python -m eval.harbor_agents.container --check
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
