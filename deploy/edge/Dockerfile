# Official multi-platform Caddy 2.11.9-alpine, immutable upstream manifest.
FROM caddy:2.11.9-alpine@sha256:0bbb055bda6793c999bde78b1b5ce3e89f829fb031af2cf50056ea1246559fee
COPY --chmod=0555 entrypoint.sh /usr/local/bin/pc-edge
USER 1000:1000
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/pc-edge"]
