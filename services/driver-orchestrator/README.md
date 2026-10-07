# driver-orchestrator

Stage 4 of the revised architecture: the per-driver join. Runs the Decision
Gateway and Post-Processing once per driver batch, never concurrently for the
same driver. See `docs/development/stages/06-driver-orchestration.md` and
`07-decision-gateway.md`; business rules follow architecture v2.1 §4.4.3 (the
Sep 29, 2026 Intake sign-off).

Built so far: `driver_orchestrator.decision` — the gateway's outcome logic as
pure functions (no Azure, Mercury or database code yet).

## Test

```bash
pip install -e ".[dev]"
pytest tests/
```
