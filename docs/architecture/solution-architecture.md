
# Solution Architecture

> **Superseded.** This describes the *original* architecture. The current source of truth is
> `DMER_Intake_Automation_Revised_Architecture.docx` (this folder) and
> [`docs/development/README.md`](../development/README.md), which documents the revised pipeline,
> data model, and Azure services for implementation. Kept for history; not updated further.

Source of truth (original): `DMER Intake Automation- Architecture (Azure Cloud) - Explained.docx` (in this folder).

This file is the living, markdown-native companion to that document — see
`repository-design.md` for the full repository/infrastructure design derived from it,
including service boundaries, Bicep module layout, naming conventions, and best practices.

## One-line summary

An AI-enabled pipeline retrieves DMER documents from Mercury (Dynamics), performs OCR
(Azure Document Intelligence), normalization (Azure OpenAI — hosted in a separate Azure
AI Hub subscription and consumed as an external endpoint, not provisioned by this
repository; see `repository-design.md` §10, §13), and rule-based evaluation (GoRules/Zen),
writing decisions and audit data back while Mercury remains the system of record for
documents and case management.

## Update policy

Any change to service boundaries, queues, or the Bicep module list must be reflected here
and in `repository-design.md` in the same PR.
