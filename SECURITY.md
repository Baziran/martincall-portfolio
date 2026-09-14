# Security and local credentials

MartinCall is a local research and paper-trading application. Its default application and database port bindings use loopback. It is not designed to be exposed as an unauthenticated public service.

## Configuration

- Keep real database passwords and provider/API tokens in ignored local files: `.env` and files inside `secrets/`.
- Commit only configuration templates such as `.env.example` and `secrets/*.env.example`. Replace their placeholders before use; do not reuse example or CI test credentials.
- Environment files, the secrets directory and Git history are excluded from the Docker build context. Files mounted at runtime are not part of the image.
- Docker Compose requires `MARTINCALL_POSTGRES_PASSWORD` explicitly and has no password fallback. Its configuration test checks every reference for this requirement without embedding a real password.
- Never paste a populated connection URL, token or environment file into an issue, README, screenshot or build log.

For a new installation, generate a unique database password and store it in the local environment. For an existing database, changing `.env` alone does not change the database role password: update the database and its clients together during an operator-controlled maintenance window.

## Public source snapshots

The portfolio repository contains a selected current source snapshot, not the private development history. Local credentials, runtime databases, backups and historical experiment exports are not included. This keeps the working installation separate from the public code sample.

Before publishing an update, inspect the exact staged files and check them for real credentials. `.gitignore` does not remove a file already tracked by Git. Removing a credential from the latest source does not remove it from older commits. Rotate any credential that has been exposed outside its intended private boundary.

This document describes configuration and publication practices, not a claim of a completed security certification or penetration test.

## Reporting

Please report a suspected credential disclosure privately to [Grigory Shmykov](https://www.linkedin.com/in/grigory-shmykov-63b11016b/). Do not include a working credential in a public issue.
