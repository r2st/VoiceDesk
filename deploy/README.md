# Deploying VoiceDesk to a Hetzner VPS

Everything here targets a single Ubuntu 22.04/24.04 VPS (a Hetzner CX22 or
larger is comfortable). Postgres, Redis and MinIO run as containers on the
same box; there is no external managed service to provision first.

## Layout

| File | Purpose |
|---|---|
| `docker-compose.prod.yml` | The full stack: postgres, redis, minio, api, worker, dashboard, caddy |
| `Caddyfile` | Reverse proxy + automatic Let's Encrypt TLS for both hostnames |
| `.env.production.example` | Template for `.env` — every secret and setting the stack reads |
| `provision.sh` | One-time VPS setup: Docker, firewall, systemd units |
| `deploy.sh` | Build, migrate and (re)start the stack — run for every release |
| `backup.sh` | Nightly Postgres dump, run by `voicedesk-backup.timer` |
| `systemd/` | Unit files installed onto the VPS by `provision.sh` |

## First deploy

1. **Point DNS at the VPS.** Create A/AAAA records for your dashboard
   hostname (e.g. `app.example.com`) and API hostname (e.g.
   `api.example.com`) pointing at the VPS's IP. Caddy cannot issue a
   certificate for either until this resolves.

2. **Copy the repo to the VPS** and provision it:

   ```bash
   rsync -a --exclude .git ./ root@<vps-ip>:/opt/voicedesk/
   ssh root@<vps-ip> 'bash /opt/voicedesk/deploy/provision.sh'
   ```

   This installs Docker, opens the firewall for SSH/80/443 only, and
   registers (but does not start) the systemd units.

3. **Fill in the environment**, on the VPS:

   ```bash
   cd /opt/voicedesk/deploy
   cp .env.production.example .env
   chmod 600 .env
   $EDITOR .env   # every value — see the comments in the file
   ```

   Generate the two secrets it asks for:

   ```bash
   openssl rand -base64 32                                          # POSTGRES_PASSWORD, REDIS_PASSWORD, S3_SECRET_KEY
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"    # JWT_SECRET, WEBHOOK_HMAC_SECRET, RECORDING_ENCRYPTION_KEY
   ```

   `DATABASE_URL` / `DATABASE_URL_SYNC` / `REDIS_URL` embed the same
   passwords you set for `POSTGRES_PASSWORD` / `REDIS_PASSWORD` — keep them
   in sync; the app reads the URLs, compose reads the bare passwords.

4. **Deploy:**

   ```bash
   cd /opt/voicedesk/deploy
   ./deploy.sh
   ```

   This builds the images, brings up postgres/redis/minio and waits for them
   to report healthy, runs the pending Alembic migration (baked into the
   `api` container's entrypoint), starts everything else, and waits for
   `/health/ready` before declaring success.

5. **Enable the stack at boot** and the nightly backup:

   ```bash
   systemctl enable voicedesk.service
   ```

   (`voicedesk-backup.timer` was already enabled by `provision.sh`.)

## Every subsequent release

```bash
cd /opt/voicedesk
git pull
./deploy/deploy.sh
```

`deploy.sh` is idempotent — the migration step no-ops when already at head,
and `docker compose up -d` only recreates containers whose image or config
actually changed. Each container's own `restart: unless-stopped` policy
keeps it running across an unattended VPS reboot; `voicedesk.service` exists
so the *stack as a whole* comes back the same way if the box reboots before
any container has had a chance to start on its own.

## Operating

```bash
systemctl status voicedesk.service          # is the stack up?
systemctl restart voicedesk.service         # full stack restart
docker compose -f docker-compose.prod.yml --env-file .env logs -f api
docker compose -f docker-compose.prod.yml --env-file .env ps
```

**Backups** land in `/opt/voicedesk/backups/voicedesk-<timestamp>.sql.gz`,
pruned after 14 days (`BACKUP_RETENTION_DAYS` in `backup.sh` if you want a
different window). Run one by hand with `./backup.sh`. Restore with:

```bash
gunzip -c backups/voicedesk-20260810T023000Z.sql.gz | \
  docker compose -f docker-compose.prod.yml --env-file .env exec -T postgres \
  psql -U voicedesk voicedesk
```

**Rolling back** a bad release: `git checkout <previous-sha>` then
`./deploy/deploy.sh` again. This only rolls back application code — a
migration that already ran against production data is not automatically
undone, so a release with a destructive migration needs its own judgment
call, not just a `git checkout`.

## Why these choices

- **Caddy, not nginx+certbot** — one process gets TLS issuance and renewal
  for both hostnames with a 30-line config and no cron job to forget about.
- **One `voicedesk.service` wrapping compose, not one unit per container** —
  `docker-compose.prod.yml` already encodes the dependency graph
  (`depends_on` + healthchecks); a systemd unit per container would either
  duplicate that graph or fight it. Each container's `restart:
  unless-stopped` is what actually recovers a crashed process; the systemd
  unit's job is only "is the stack running at all," which is a single
  question with a single unit.
- **Migrations run from the API container's entrypoint, not a separate
  step** — a deploy that starts `api` without its matching schema is exactly
  the failure mode to prevent, so the migration and the server that depends
  on it share one startup path instead of two that can drift apart.
