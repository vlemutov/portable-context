# Automatic edge–VM certificate renewal (1.0 preview)

Cloud.ru manages the public HTTPS certificate. Portable Context renews the two
private trust roots and the client/server certificates used **between the edge
container and the owner's VM**. This operation does not reset OAuth, change the
memory workspace, migrate files or replace the VM.

The controller runs on the Linux VM. The owner's computer can be offline. A
systemd user timer checks every ten minutes and catches up after a missed timer
event. The installer verifies user lingering and enables it for the current owner
with noninteractive sudo when needed; without that prerequisite it reports failure,
not a ready timer. Certificates last at most 90 days; renewal starts 30 days
before the earliest expiry. Status requests attention below 14 remaining days or
after an operation failure. Before the renewal window, the timer performs only a
local status check, with no Cloud.ru API request.

## Install on an existing HTTPS/OAuth deployment

Use the same private deployment state and current certificate generation. The
reviewed non-secret JSON configuration identifies the existing project, container,
public hostname, VM address, pinned owner-registry image, and loopback ports:

```json
{
  "project_id": "PROJECT-UUID",
  "container_name": "YOUR-EDGE-NAME",
  "container_id": "CONTAINER-UUID-FROM-CREATION-RECEIPT",
  "public_host": "YOUR-EDGE.containerapps.ru",
  "origin_ip": "YOUR-VM-IP",
  "image": "YOUR-REGISTRY.cr.cloud.ru/edge-runtime@sha256:IMAGE-DIGEST",
  "origin_port": 9443,
  "application_port": 8767,
  "identity_port": 14120
}
```

```sh
python deploy/setup.py --state /PRIVATE/deployment rotation-setup \
  --config /PRIVATE/rotation-options.json --generation /PRIVATE/current-generation
```

This installer currently enrolls the existing preview layout: the origin is
`gateway10/caddy` with `gateway10/keys/origin.Caddyfile` inside the server root.
It checks the memory workspace ID, origin certificate, edge certificate,
container ID, image and public address before scheduling. It never recreates a
container by name or chooses a different public URL. A repeat installation keeps
the current generation and unfinished operation; changed settings or credentials
require a reviewed migration. Full fresh HTTPS/OAuth provisioning remains part
of the coordinated 1.0 installer work.

The installer transfers Cloud.ru API credentials and current transport keys over
SSH into a private `gateway-rotation` directory on the owner's VM. **This is a
secret directory.** Use a dedicated cloud account limited to the deployment's
Container Apps and Secret Management operations where available. Neither cloud
credentials nor memory enter the edge image. Only the edge client key is placed
in Cloud.ru Secret Management. The timer uses the owner's supplied credentials;
it cannot renew them if the cloud account/key itself expires or loses permissions.

The result contains the server state path, executable, script and timer name.
Use those exact returned paths for owner-operated status and diagnosis:

```sh
SERVER_PYTHON SERVER_SCRIPT --state /SERVER/gateway-rotation status
SERVER_PYTHON SERVER_SCRIPT --state /SERVER/gateway-rotation run
```

`run --force` starts a renewal before it is due, for acceptance testing. Once a
rotation is pending, subsequent runs always resume it, including after `--force`.
Do not delete the state directory or issue another generation to repair a retry.

## Switching protocol

1. Persist a new private generation and the operation intent.
2. Reload the VM proxy with the **old server certificate and both client roots**.
3. Publish the edge with the **new client certificate and both server roots**.
4. Observe the new revision over the real mTLS path; allow old requests to drain.
5. Reload the VM proxy with the **new server certificate and both client roots**.
6. Allow existing connections to finish.
7. Publish the edge trusting only the new server root; then remove the old client
   root from the VM. Verify a fresh connection succeeds and an old client is refused.
8. Commit the new active generation and next renewal date.

Each draining interval defaults to 900 seconds and cannot be shorter than 660
seconds, exceeding the configured 600-second edge request limit. A rotation can
therefore take more than half an hour with cloud publication and timer intervals.
The old working trust remains during waiting periods. Historical Cloud.ru
revisions can remain `ready`; that label alone is not proof of current traffic.
The controller checks the served revision and end-to-end origin stamp as well.

The VM proxy uses validated immutable key bundles and Caddy's `SIGUSR1` graceful
reload. It retains the listener and ongoing requests; no public admin endpoint
is enabled. It refuses a different process/configuration or an unrelated manual
configuration change. A coordinated updater must update the controller's bindings
when changing image, ports or service layout.

## Interruption and diagnostics

Intent, complete file sets and live configuration pointers are persisted before
effects. Files and containing directories are synced on Linux. After a process
dies, the same stage is replayed. Partial key pairs are never installed.

Secret names are deterministic per generation. A lost creation response is
resolved by reading and comparing the existing payload; collisions are rejected.
A lost container PATCH response is reconciled against the current container
specification and the actually served edge. Reapplication of the same desired
template is bounded, follows a fresh read and a five-minute quiet period, and
stops on unrelated changes. It never deletes or replaces a container to recover.

Errors leave the journal and working overlap intact. Status contains dates,
generation/stage and redacted failure guidance, never keys or API responses.
Old key files and Secret Management entries are retained for owner investigation;
they are no longer trusted after successful completion. Do not delete active or
pending generations. Preserve the private controller state across upgrades.

This is continuity during planned rotation and controller interruption, not an
availability guarantee for a failed VM or cloud platform. If the controller is
offline or denied cloud access until certificates expire, explicit owner recovery
may be needed. TLS verification is never disabled to bypass expiry. Reconstructing
a completely lost VM's transport configuration is part of server recovery/setup,
not a reason to restore expired credentials silently.

## Acceptance coverage

Unit tests exercise every stage and ambiguous cloud replies. The Linux acceptance
test uses actual Caddy and a systemd origin, kills controller processes before and
after each stage, resumes from disk, continuously checks requests, preserves a
long write and streaming response, and rejects the retired client certificate.
It uses a local edge to isolate the protocol. Real Cloud.ru publication and its
availability monitoring are verified separately on the pilot; local tests must
not be reported as a cloud rolling-deployment guarantee.

Pilot result (2026-11-10): a real Cloud.ru rotation completed after the controller
was killed immediately after a successful container PATCH, before recording the
reply. The resumed operation reused the generation, retired the old certificate
and preserved the OAuth workspace. Out of 1,311 public checks, two returned HTTP
503 during the final revision switch. Thus automatic renewal and recovery passed,
but **uninterrupted public access did not pass** with the pilot's 0–1 instance
configuration. Do not advertise zero downtime. A warm-instance configuration is a
separate paid availability experiment, not an established fix. The controller
must not conceal this with automatic retries of memory mutations.

The real systemd timer was also started explicitly after installation. Its command
must preserve the virtual environment's Python path rather than resolving its
symlink to the system interpreter, which would lose application dependencies.

References:
- [Caddy signals and graceful reload](https://caddyserver.com/docs/command-line#signals)
- [Cloud.ru Container Apps API](https://cloud.ru/docs/container-apps-evolution/ug/topics/api-ref)

## Safe continuity diagnostics

Run finite, read-only probes from the client before starting a separately authorized
rotation. The output filename must be new; old evidence is never overwritten:

```sh
python deploy/gateway_diagnostics.py --public-origin https://YOUR-EDGE.containerapps.ru \
  --output /PRIVATE/probe-run/public.jsonl --seconds 2400
```

On the VM, use the controller's installed Python and `gateway_diagnostics.py` from
the same release directory. Add `--state /SERVER/gateway-rotation` and another
output file. This mode probes the origin over mTLS and the loopback MCP process,
independently of the public Cloud.ru route. It uses existing private transport
credentials locally; credentials are not copied into the trace.

The independent Python VM probe pins the exact issued origin leaf certificates
for the active/pending generations using OpenSSL partial-chain verification.
Hostname, expiry and TLS key-possession checks remain enabled; unrelated and
retired certificates are rejected. This avoids ambiguous issuer selection in
historical CA bundles whose generations share a subject name. It does not change
the production Caddy trust configuration or disable TLS verification.

The four public probes check edge health, the complete mTLS path, static MCP
resource metadata, and the expected unauthenticated OAuth challenge. Each probe
is one fixed-path GET: no arbitrary URL paths, bearer tokens, redirects, memory
mutations or automatic request retries. Regular probes keep a cold container
active while the test runs; they do not change its scaling settings. Stop them
after the acceptance window instead of running an unrequested permanent monitor.

JSONL contains UTC request start/end, duration, status, result of fixed-response
validation, TLS peer certificate fingerprint, and a positive allowlist of headers:
known transport revision markers, the fixed failure layer, strictly shaped
provider request IDs, and known server names. No body, body hash, cookies,
authorization headers, raw exception message, query string, source IP or memory
content is recorded. Unknown header values are discarded. Files are created
privately, capped at 16 MiB, and the summary reports dropped records or write
failure; a truncated trace must not be called complete evidence. Windows inherits
the destination directory ACL, so use the private deployment directory.

Versioned proxy configurations now return `X-PC-Edge-Revision` and
`X-PC-Origin-Revision`. An internal proxy failure adds `X-PC-Failure-Layer: edge`
or `origin` with a fixed generic body and the original HTTP error status. An
application-generated 503 is forwarded unchanged, without a proxy-failure marker.
General Caddy request/error logging remains off: capturing arbitrary requests
would undermine the diagnostic privacy boundary.

Interpret simultaneous evidence, not one missing header:

- Edge health fails while the independent VM probes remain healthy: investigate
  the public route or edge runtime, not memory recovery.
- Edge health succeeds, but the transport probe fails with the edge-failure
  marker: investigate the edge-to-VM connection, including TLS.
- Transport succeeds, but metadata fails with an origin-failure marker: investigate
  the VM proxy-to-MCP connection and compare the direct loopback probe.
- An upstream HTTP error with both revision markers and no proxy-failure marker
  is distinct from a proxy exception. Correlate it with the direct MCP probe.
- Missing markers alone do not prove a provider fault: older revisions and header
  processing can also omit them. Preserve the provider request ID for investigation.

Record cloud publication status and controller stage timestamps alongside these
traces. Do not infer an outage duration from the time between two sampled errors,
or equate the provider's operation-complete flag with verified request readiness.

### Instrumented pilot repeat (2026-11-10)

The repeat kept min/max instances at 0/1, CPU at 0.1 and memory at 256 MiB.
Rotation recovered the same generation after an actual controller exit immediately
following a successful cloud PATCH. It completed at 18:09:03 UTC, and the real
scheduled service subsequently returned `not_due` with an enabled, active timer.
The authenticated OAuth workspace remained accessible with unchanged message,
original and wiki counts. Both finite monitors were stopped after the test.

Public probes ran from 17:41:16 to 18:10:05 UTC: **4,234 requests, three failures**:

- A 504 on `/mcp` at 17:45:50 UTC coincided with the first observed new edge
  revision. It had no allowlisted diagnostic headers. Concurrent edge, transport
  and metadata probes succeeded, including responses from both edge revisions.
- Two public TLS-handshake failures started at 18:09:32 and 18:09:33 UTC, after
  rotation completed. Concurrent edge and OAuth-challenge probes succeeded;
  independent origin-mTLS and MCP-loopback probes also succeeded. These failures
  occurred before HTTP and do not establish whether the cause was the client's
  network path or the provider's public TLS endpoint.

Coverage limitations are retained in the evidence. The first VM monitor failed
to start because of an unnecessary application dependency (removed and covered by
a standard-library-only subprocess test); independent VM coverage began at
17:46:33, after the 504. Its first TLS implementation then produced 178 false
verification failures after the origin switched certificates: the historical
same-subject CA ambiguity described above was reproduced with synthetic TLS and
fixed. The corrected probe ran from 17:59:37 through 18:10:19 UTC, covering final
publication and both public TLS failures: **1,255 VM checks, zero failures**.
Original and corrected traces remain separate. No records were dropped.

The provider system-log API still returned only previous-day events. Therefore
the 504's internal cause is unresolved; no claim that paid warm instances would
fix it is justified. The next diagnostic comparison should use a second public
observer on an independent network, alongside the existing VM probes, and obtain
current provider ingress/revision events. Never replay memory mutations to hide
these failures. This pilot still does **not** establish uninterrupted public access.
