FROM caddy:2.11.9-alpine@sha256:0bbb055bda6793c999bde78b1b5ce3e89f829fb031af2cf50056ea1246559fee AS runtime
# Reset inherited image metadata: Timeweb discovers every EXPOSE from the parent,
# including Caddy's unused 80/443/2019 ports. Preserve the pinned runtime filesystem.
FROM scratch
COPY --from=runtime / /
COPY --chmod=0555 entrypoint.sh /usr/local/bin/pc-edge
ENV XDG_CONFIG_HOME=/tmp/pc-edge/config XDG_DATA_HOME=/tmp/pc-edge/data
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 CMD ["/bin/sh", "-c", "wget -q -O /dev/null http://127.0.0.1:8080/_pc/health || exit 1"]
ENTRYPOINT ["/usr/local/bin/pc-edge"]
