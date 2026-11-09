# Portable Context 1.0 — implementation and release gates

Status: in development. The production memory remains on 0.9.0. Do not advertise
OAuth or the new HTTPS installation as released until the acceptance gates pass.

## Agreed deployment

The owner does not have to buy a domain. For Cloud.ru, evaluate its managed
Container Apps HTTPS address with a **stateless edge only**. Keep the existing VM,
SQLite, originals, import jobs, wiki, backup schedules and restic repository.
Own-domain and own-server installation remain supported targets. No shared SaaS.

```
Agent -- public HTTPS --> Cloud.ru Container Apps / Caddy
      -- mutually authenticated TLS --> Caddy on owner VM
      -- loopback --> authenticated MCP / identity service
                         | SQLite + jobs on VM
                         | S3 originals + restic backups
```

The edge certificate authenticates a machine, not the user. OAuth/device
authorization remains mandatory at the application. Do not expose unprotected
MCP while implementing this transport. Probe the gateway with synthetic data first.

## Edge implementation

- `deploy/gateway.py`: validate destinations, generate separate server/client
  trust roots, render Caddy configuration. Never disable TLS verification.
- `deploy/cloud_gateway.py`: official Cloud.ru API adapter and bounded container
  request. References a pinned owner-registry image and a Secret Management key.
- `deploy/edge`: pinned upstream Caddy image, unprivileged entrypoint, no volumes
  or memory in the image; secrets materialize only in temporary runtime files.
- Origin and edge check the public Host; SNI and Host match. Drop untrusted
  forwarded headers; do not infer a real user's IP from provider forwarding yet.
- 16 MB request limit (existing captures use smaller resumable chunks), no proxy
  retry of failed mutations, unbuffered responses, 600 s provider request limit.
  Durable imports must outlive HTTP disconnects on the VM, not in the edge.
- Minimum zero / maximum one replica, 60 s idle timeout, 0.1 vCPU / 256 MiB initial
  candidate. Validate actual provider minima and workload before claiming costs.
- No request/access logs containing authorization, cookies, query strings or
  transcripts. Health probe reports only process liveness, not memory readiness.

Private key generations have at most 90-day validity. **Automatic rotation and
restart/overlap recovery are a release gate**, not implemented by `prepare`.
Never overwrite a live key generation during a repeated installation.

## Remaining 1.0 gates

1. Real Cloud.ru test: stable HTTPS URL through revision update/restart, unprivileged
   execution, streaming, 1 MiB capture chunks, timeouts, cold starts, mTLS to VM,
   no public bypass, cost report including registry and secrets. Do not alter SSH.
2. Tested maintained OAuth provider, PKCE/discovery, application capability checks
   for every MCP operation, device fallback, revocation including existing sessions.
3. Owner setup/recovery, no public first-visitor-wins registration. Auth state
   backup/restore must not resurrect revoked credentials.
4. Friendly repeatable install/update/status/diagnostics and existing-vs-new memory
   onboarding on Windows/macOS/Linux. No claim of a platform test without execution.
5. First useful import, wiki evidence and cross-agent recall over the new connection;
   old SSH path remains recoverable until the replacement is verified.

Sources checked 2026-11-09:
- https://cloud.ru/docs/container-apps-evolution/ug/topics/guides__container-create
- https://cloud.ru/docs/container-apps-evolution/ug/topics/api-ref
- https://cloud.ru/docs/container-apps-evolution/ug/topics/concepts__volumes
- https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
- https://caddyserver.com/docs/caddyfile/directives/tls
