# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Email **narender.rk10@gmail.com** with "ArchFlow security" in the subject,
a description of the problem, steps to reproduce and the affected version or commit. You will get an
acknowledgement within 3 working days and a fix or mitigation plan as soon as the issue is confirmed. Please give
us a reasonable time to release a fix before disclosing it.

## Supported versions

Security fixes go to the latest release and the `main` branch.

## How ArchFlow protects data

- Passwords are hashed with scrypt; sessions are HttpOnly cookies; unsafe requests need a trusted `Origin` (CSRF).
- Organization AI keys are encrypted at rest (AES-256-GCM, `ARCHFLOW_SECRET_KEY`); only the last 4 characters are
  ever shown.
- OAuth codes and API tokens are stored as SHA-256 hashes only; MCP access uses OAuth 2.1 with scoped tokens.
- Repository analysis refuses private, loopback and link-local addresses (SSRF) and runs in a worker process.
- AI traces can be stored without prompt or response text (`ARCHFLOW_TRACES=metadata`) and are pruned after
  `ARCHFLOW_TRACE_RETENTION_DAYS`.

## Deployment checklist

Set `ARCHFLOW_SECRET_KEY`, `ARCHFLOW_INTERNAL_TOKEN` and `POSTGRES_PASSWORD` to strong random values, set
`ARCHFLOW_PUBLIC_URL` and `ARCHFLOW_ALLOWED_ORIGINS` to your domain, serve over HTTPS, keep
`ARCHFLOW_ALLOW_LOCAL_REPOS` and `ARCHFLOW_ALLOW_PRIVATE_AI_URLS` off on shared servers, and back up the database
together with the secret key. See [docs/deploy.md](docs/deploy.md).
