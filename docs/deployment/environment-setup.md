# Environment Setup

## Prerequisites (provisioned manually — not in Bicep)

- Resource groups (`rg-rsbc-dmer-dev`, `rg-rsbc-dmer-test`, `rg-rsbc-dmer-prod`)
- Landing zone VNet, subnets, flow logs, and Network Watcher (Platform team)
- Azure subscription + RBAC role assignments for the deployment service principal
- Document Intelligence custom model training/publishing

## Per-environment configuration

Environment-specific values (SKUs, scaling limits, allowed IPs) live in
`deployment/<env>/parameters.json`. Never hardcode environment values in
`infrastructure/bicep/modules/*`.

## Secrets

All secrets are stored in Key Vault and referenced by App Configuration / service
Managed Identities. No secret is ever committed to this repository.

### External Azure OpenAI endpoint

`normalizer-service` calls a model hosted in a separate Azure AI Hub/AI Foundry project, in
a separate Azure subscription owned by another team — the model is already deployed there;
we do not provision or manage it. Per environment, a Key Vault secret holds the endpoint URL
and API key for that project's OpenAI resource, and `normalizer-service` reads it via
`libs/dmer_common/config`. This is the one intentional exception to "no secrets, Managed
Identity everywhere" — see `docs/architecture/repository-design.md` §10 and §13 (item 8).
