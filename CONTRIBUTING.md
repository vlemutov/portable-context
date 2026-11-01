# Contributing

Start with a small synthetic fixture reproducing a provider format difference. Preserve original
fields, stable source identity, branches and timestamps. Do not silently coerce unknown schemas
into success. Document whether a feature is archive import, live capture, or agent-triggered memory.

Run `python -m unittest discover -s tests -v`. Install `.[mcp]` to include protocol integration tests.
Keep data directories, credentials and real exports outside the repository. Check staged files
before publishing. Tests should use a temporary directory and leave user configuration alone.

Current priorities: real anonymized export validation; paginated Codex app-server exporter;
version-specific Ouroboros/Hermes/OpenClaw hooks; incremental large-file ingestion; explicit
cross-source mappings; deletion/retention; optional embeddings and evidence-grounded extraction.
