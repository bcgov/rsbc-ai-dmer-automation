# Debug scripts

Ad hoc tools for inspecting live Azure resources during development, run
from wherever actually has network access to reach them (a jump box inside
the VNet, in most cases here). Not part of any service's own deployable
code, and not exercised by any service's own build/test/deploy pipeline —
these are ops/debug tooling, not application code.

- `sb_queue_viewer.py` -- desktop GUI for peeking/receiving Service Bus
  queue messages (`dmer-ingest`, `dmer-raw`, `dmer-extracted`) on the private,
  publicNetworkAccess=Disabled namespace, since the Azure Portal's own
  Service Bus Explorer refuses to operate against it at all regardless of
  network path. See the script's own docstring for its Peek vs. Receive
  modes and why it needs to run somewhere with a usable managed identity.

  Provisioned automatically onto the jump box VM by
  `scripts/deployment/jumpbox-setup.sh` (see
  `infrastructure/bicep/jumpbox.bicep` and
  `docs/deployment/jumpbox-setup.md`) — that's IaC for *deploying* this
  file onto a fresh VM, not a claim that it's part of the DMER pipeline
  itself.
