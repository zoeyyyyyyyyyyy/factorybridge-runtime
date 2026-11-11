# FactoryBridge runtime

A clean, independently runnable snapshot of the FactoryBridge coordination core: an LLM-reviewed planner, supplier marketplace, simulated manufacturing/inspection/assembly/shipping, and independent delivery checker. This export contains no benchmark cases, fresh inputs or raw logs, credentials, local budget journals, UI, CAD tool containers, or original Git history.

Source: FactoryBridge `55fd8d81d52cf554745b91e250e5fa74842fd49e`. `EXPORT_MANIFEST.json` records the original source hashes. The core planner, marketplace, simulator, wire types and checker are unchanged. Only the arbitrary synthetic supplier fixture was extracted into a small resource-backed module; a compatibility import preserves the original Town smoke test.

## Run

Python 3.11+; the runtime uses the standard library. The Town integration installs its own dependencies separately.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

The matching Town PR contains the HTTP coordinator/requester/participant adapter and reproducible commands. Real-model tests require **your own** model credentials via environment variables or an ignored local key file. No key is shipped. Scripted tests are explicitly identified and do not establish live-model behavior.

This is the coordination service used in the course experiment, not a physical manufacturing service. The provided catalog and plate estimates are synthetic test fixtures; successful checks do not certify real dimensions, strength, payment, printing or delivery. The FactoryOS CAD/slicing pipeline and production deployment remain outside this runtime's scope.

## Data boundary

Only allowlisted source files and the synthetic integration fixture were copied. Fresh evaluation data, benchmark input/answer files, experiment archives, API keys, database/session files, uploads and private documents were excluded. Preserve this boundary when contributing.

MIT License; original FactoryOS/FactoryBridge contributor attribution is retained in `LICENSE`.
