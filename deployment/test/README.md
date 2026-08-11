
# test deployment parameters

Environment-specific values only (SKUs, scaling limits, Document Intelligence resource
names, allowed subnets). Deployed against `infrastructure/bicep/main.bicep` —
see `docs/deployment/deployment-guide.md`.

Note: this file does *not* hold Azure OpenAI values — that model is hosted in a separate
AI Hub subscription and consumed via a Key Vault secret (endpoint + API key), not a Bicep
parameter. See `docs/architecture/repository-design.md` §10, §13.
