
# Deployment Guide

## Environments

| Environment | Purpose | Approval |
|---|---|---|
| dev | Continuous deployment from `main` | Automatic |
| test | Pre-production validation, UAT | 1 approver |
| prod | Production | 2 approvers, change window |

## Deploying infrastructure

Infrastructure is deployed with `az deployment group create`, targeting the pre-existing
resource group and VNet (both created manually — see
`docs/architecture/repository-design.md` for the list of manually-provisioned resources).

```bash
az deployment group create \
  --resource-group rg-dmer-<env>-cac \
  --template-file infrastructure/bicep/main.bicep \
  --parameters deployment/<env>/parameters.json
```

Run `az deployment group what-if` first for `test` and `prod`.

## Deploying services

Each service is built independently (Docker image for Container Apps, zip package for
Function Apps) and deployed via its stage in the corresponding `.github/workflows/deploy-*.yml`
pipeline. See each service's `README.md` for its build command.

## Rollback

See `rollback-procedures.md`.
