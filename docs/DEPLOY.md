# Deploying LedgerLine

You need a PostgreSQL database (14 or newer; 16 recommended) and somewhere to run a Python web service. LedgerLine needs `CREATE` rights on its own database (it makes a schema, tables, triggers and functions). It does **not** need superuser.

## Option A: Render (free, about 10 minutes)

1. Push the repository to GitHub (public).
2. On [render.com](https://render.com), choose **New → Blueprint**, pick the repository, and accept `render.yaml`. It creates:
   * `ledgerline-db`, a free PostgreSQL 16 database;
   * `ledgerline`, a Python web service, with `DATABASE_URL` wired to the database and `SECRET_KEY` and `LEDGER_SIGNING_KEY` generated randomly.
3. Optional: in the web service's **Environment** tab, set `ANTHROPIC_API_KEY` to enable LLM review notes. Without it, notes use the built-in template.
4. Wait for the first deploy. The start command runs `migrate` (creates the schema and the signed GENESIS entry), then `seed --if-empty` (demo users and accounts), then starts two uvicorn workers.
5. Open `https://<service>.onrender.com`, sign in with one of the demo accounts listed in the README (or create a user), and put that URL in the README.
6. Check the live system: `python tools/attack.py --url https://<service>.onrender.com`

Notes:
* Free web services sleep after a period of inactivity; the first request then takes about a minute. Open the site before a judge does.
* Free Render databases are deleted after their free period. Note the date and export an evidence bundle (Proof → Download evidence bundle).
* **Never change `LEDGER_SIGNING_KEY` after the first deploy**: older entries would no longer verify.
* `.python-version` pins Python 3.13, the version the tests were run on.

## Option B: any Docker host

```bash
docker build -t ledgerline .
docker run -p 8000:8000 \
  -e DATABASE_URL=postgresql://USER:PASS@HOST:5432/DB \
  -e APP_ENV=production \
  -e SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))") \
  -e LEDGER_SIGNING_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))") \
  ledgerline
```

## Option C: Render web service + Neon database

Create a free database on [neon.tech](https://neon.tech) and copy the **direct** (non-pooled) connection string; LedgerLine sets `search_path` per connection, which a transaction-pooling proxy does not keep. Create a Render **Web Service** from the repo with:

* Build: `pip install -r requirements.txt`
* Start: `python -m app.cli migrate && python -m app.cli seed --if-empty && uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2 --proxy-headers --forwarded-allow-ips "*"`
* Env: `DATABASE_URL`, `APP_ENV=production`, `SECRET_KEY`, `LEDGER_SIGNING_KEY`

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | yes | PostgreSQL connection string (`postgres://` or `postgresql://`) |
| `APP_ENV` | yes in prod | `production` turns on safety checks (no default secret, no test hooks) and trusts the proxy's last `X-Forwarded-For` hop |
| `SECRET_KEY` | yes in prod | Signs login tokens; at least 32 characters |
| `LEDGER_SIGNING_KEY` | recommended | Seed for the Ed25519 journal-signing key; set once, never change |
| `ANTHROPIC_API_KEY` | no | Enables LLM review notes and summaries |
| `AI_MODEL` | no | Default `claude-haiku-5-5` |
| `ACCESS_TOKEN_MINUTES` | no | Default 60 |
| `LOCK_TIMEOUT_MS` | no | Default 5000 (how long a request waits for a busy row or key) |
| `TRUST_PROXY` | no | Override proxy trust (`1`/`0`) |
| `SIGNUP_MAX` / `SIGNUP_WINDOW_SECONDS` | no | Public sign-up limit per address (default 10 per 600 s) |

## Operating it

* `python -m app.cli verify`: run the full proof from a shell (exit code 1 if any check fails).
* `python -m app.cli export evidence.json`, then `python tools/verify_bundle.py evidence.json --public-key <key>` to verify offline.
* `python -m app.cli keygen` prints a random value suitable for `SECRET_KEY` or `LEDGER_SIGNING_KEY`.
* Optional hardening: create a separate database role for the app that cannot `ALTER` the tables (so it cannot disable the append-only triggers). The tables would then be owned by a migration role. Render's single-user databases make this awkward, so it is documented rather than wired in (THREATS A2).
