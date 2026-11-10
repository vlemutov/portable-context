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
