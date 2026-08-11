
# Infrastructure (Bicep)

`main.bicep` composes the reusable modules in `modules/` into the full environment.
Per-environment values live in `deployment/<env>/parameters.json` (not here) — this
folder contains templates only, per "do not duplicate templates".

Manually-provisioned resources (not defined anywhere in this folder): resource groups,
developer VMs, Document Intelligence custom model training, the landing zone/subscription,
the VNet/subnets/flow logs/Network Watcher, and RBAC assignments beyond what's needed for
Managed Identity access to the resources this template creates.

See `docs/architecture/repository-design.md` for the module list, resource naming
convention, and dependency diagram.
