# LATAM jobs

Personal collector for remote software jobs that a resident of one Latin American country can take. The default residence country is Colombia.

Configuration, collection, and the local dashboard are documented in this file as the pieces land. Deployment steps are in the VPS section below once the systemd units exist.

```bash
uv sync
uv run latam-jobs check
```
