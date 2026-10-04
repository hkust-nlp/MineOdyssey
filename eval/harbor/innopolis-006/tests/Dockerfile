FROM localhost/anonymous-navigation:linux-cpu
COPY test.sh verify.py task-spec.json /tests/
WORKDIR /tests
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
