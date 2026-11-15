FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates iproute2 procps \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'PyJWT[crypto]==2.10.1'
RUN curl --fail --location --retry 3 https://github.com/pocket-id/pocket-id/releases/download/v2.19.2/pocket-id_linux_amd64 -o /usr/local/bin/pocket-id \
    && echo '85881ea87d78710c34f127b2d849d27dd7e69963723d9e590f66de26e87fc4fb  /usr/local/bin/pocket-id' | sha256sum -c - \
    && chmod 755 /usr/local/bin/pocket-id
WORKDIR /diagnostic
COPY server.py smoke.py VERSION ./
RUN python smoke.py
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s CMD curl --fail http://127.0.0.1:8080/health || exit 1
CMD ["python", "server.py"]
