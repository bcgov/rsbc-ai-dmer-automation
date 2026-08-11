
# Security Guidelines

- **Identity:** every service uses a dedicated user-assigned Managed Identity; least-privilege
  RBAC role assignments only (no `Owner`/`Contributor` grants to application identities).
- **Networking:** all PaaS dependencies are reached over private endpoints inside the
  platform-provided VNet; public network access is disabled at the resource level wherever
  supported.
- **Secrets:** no secrets in source control, app settings, or Bicep parameters — Key Vault
  references only. `.gitignore` blocks `local.settings.json` and `.env`.
- **Data protection:** DMER content and OCR/normalized output contain personal/medical
  information — encrypted at rest (platform default) and in transit (TLS 1.2+), with
  retention/lifecycle rules enforced on the `archive/` container.
- **Supply chain:** dependency scanning (`pip-audit`), container scanning (Trivy), and secret
  scanning (gitleaks) run on every PR via `security-scan.yml`.
- **Least privilege data access:** `audit-service` is read-only against PostgreSQL by design
  (its Managed Identity is granted `SELECT` only).
