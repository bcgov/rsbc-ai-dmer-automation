# Security Policy

This repository contains the RoadSafetyBC DMER AI automation solution. The
solution processes Driver's Medical Examination Reports, which contain personal
and medical information protected under FOIPPA, so we take security reports
seriously and handle them privately.

## Supported versions

Security fixes are made on the latest code only:

| Branch | Supported |
|---|---|
| `dev` (integration, deployed to the dev environment) | Yes |
| `main` | Yes |
| Older commits, tags and feature branches | No |

## Reporting a vulnerability

**Please do not report security vulnerabilities in public GitHub issues,
discussions or pull requests.**

Report it privately through GitHub:

1. Go to the repository's **Security** tab.
2. Select **Report a vulnerability**
   ([direct link](https://github.com/bcgov/rsbc-ai-dmer-automation/security/advisories/new)).
3. Describe the issue. Where you can, include:
   - the affected component (service, workflow, infrastructure module) and branch or commit;
   - steps to reproduce, or a proof of concept;
   - the impact you expect (for example, data exposure or privilege escalation);
   - any suggested fix.

**Do not include real personal or medical information in a report**, such as
DMER content, names, driver's licence numbers or health details. If you need to
show that data is exposed, describe what kind of data it is and where it appears.

The report is visible only to you and the repository maintainers.

## What to expect

- We aim to acknowledge your report within **5 business days**.
- We will confirm the issue, assess its impact, and keep you informed of
  progress toward a fix.
- We follow coordinated disclosure: please give us a reasonable time to fix the
  issue before disclosing it publicly. We will agree a disclosure date with you
  and credit you if you wish.

## Scope

In scope: code, configuration, CI/CD workflows and infrastructure as code
(Bicep) in this repository.

Out of scope, so please report these to their owners:

- vulnerabilities in third-party packages, which should go to the package
  maintainer (dependency updates in this repository are tracked by Dependabot);
- external services this solution calls but does not own, such as the BC Gov
  AI Hub and the Mercury system;
- the shared BC Gov Azure platform.

## Comments on this policy

Suggestions to improve this policy are welcome as a pull request.
