
# BC Government Standards Alignment

This project follows applicable BC Government digital delivery standards:

- **Digital Code of Practice** (digital.gov.bc.ca/policies-standards/dcop) — service design,
  delivery, and technology principles for BC Public Service digital products.
- **BC Government IM/IT Standards** (www2.gov.bc.ca — Find an IM/IT Standard) — applicable
  software development, security, and information management standards.
- **FOIPPA (Freedom of Information and Protection of Privacy Act)** — DMER data contains
  personal and medical information; all storage and processing must remain in Canadian
  Azure regions (Canada Central / Canada East) with data residency and access controls
  documented in the STRA/PIA for this solution.
- **BC Government API Guidelines** (github.com/bcgov/api-guidelines) — applied to
  `audit-service` and the `intake-processor` webhook surface.

## Project-specific application

- All Azure resources are deployed to Canadian regions only (see naming convention `cac`/`cae`).
- Personal information (driver medical details) is never logged in plaintext; structured
  logging redacts PII fields — see `libs/dmer_common/telemetry`.
- Security Threat and Risk Assessment (STRA) and Privacy Impact Assessment (PIA) artifacts
  for this solution are tracked outside this repository per ministry process, but any
  control they mandate (e.g. specific encryption, retention period) must be reflected in
  the relevant Bicep module and this documentation.
