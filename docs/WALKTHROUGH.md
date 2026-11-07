# Walkthrough: how LedgerLine is built, phase by phase, and how to defend it in the interview

The judges will ask you to walk through your own code and make a change live.
Read this with the code open next to it. Line numbers are approximate; search
for the quoted text.

---

## Phase 1: the database refuses to break the books (`app/schema.sql`)

Start here: this file is the last line of defence, and the judges care most about it.

| Rule | Where | What it means |
|---|---|---|
| Money is an integer | `balance BIGINT` | Paise, never floats. |
| The floor | `CONSTRAINT balance_floor CHECK (allow_negative OR balance >= -overdraft_limit)` (~line 88) | PostgreSQL itself refuses any balance under the floor, atomically, inside the UPDATE that changes it. |
| Balances only change via postings | `accounts_guard`, `pg_trigger_depth() < 2` (~line 108) | A direct `UPDATE accounts SET balance = …` raises `LL002`. Only the posting trigger (depth 2) can change it. |
| Applying a posting | `postings_apply`: `UPDATE accounts SET balance = balance + NEW.amount` (~line 286) | The only place a balance changes. |
| Stale-read tripwire | `IF actual <> NEW.balance_after` (~line 295) | The app states the balance it expects. If the database disagrees, the app read a stale balance (a missing lock), so the write is refused (`LL004`). |
| Double entry | `journal_postings_match`, deferred to COMMIT (~line 313) | A posted entry needs ≥ 2 postings that sum to 0 and credit exactly its amount. Refusals carry none. |
| Same-transaction postings | `LL008` in `postings_apply` | Nobody can bolt postings onto an old entry later. |
| Append-only | `journal_append_only`, `postings_append_only` (`forbid_mutation`) | UPDATE/DELETE/TRUNCATE raise `LL010`. |
| Gapless chain | `journal_before_insert`, `ledger_head_guard` | Every entry must extend the head by exactly one; no back-dating. |
| Uniqueness | `journal_idempotency`, `journal_one_decision_per_hold`, `journal_one_reversal_per_entry` | One entry per (user, key); one decision per hold; one reversal per entry. |
| Exactly-once storage | `idempotency_keys` + `idempotency_write_once` | Completed once, never edited or deleted. `response` is TEXT so replays are byte-identical. |

System accounts: `SYS-EXTERNAL` (the outside world; the only account allowed below zero) and `SYS-SUSPENSE` (payments held for review).

## Phase 2: the money engine (`app/ledger/`)

* `engine.py`: **every rupee moves through here.** `run()` is the envelope (transaction + idempotency). `deposit`, `withdraw`, `transfer`, `decide_hold` and `reverse` are the operations.
* `journal.py`: `append_entry()`, the **only** writer of journal rows and postings. It locks the chain head, computes each posting's expected balance, hashes, signs, inserts and advances the head.
* `accounts.py`: `lock()` takes `SELECT … FOR UPDATE` on customer accounts **in ascending id order**.
* `idempotency.py`: claim (INSERT first), complete (store the exact response), answer duplicates.
* `proof.py`: replays the whole journal and runs 13 checks. `statements.py`: statements. `admin_ops.py`: open, freeze, overdraft, users. `ai_notes.py`: puts AI output on the record.
* `app/signing.py`: canonical JSON, SHA-256 and Ed25519.

The shape of every money request (top of `engine.py`):

```
BEGIN
  1. INSERT idempotency key            (duplicate? wait for the first, then replay)
  2. authorise on immutable facts      (role, owner)                      -> refuse + journal
  3. lock customer rows, ascending id  (SELECT ... FOR UPDATE)
  4. check status, daily limit, BALANCE                                    -> refuse + journal
  5. Sentinel score                                                        -> maybe HOLD
  6. append_entry in a SAVEPOINT       (chain-head lock, journal, postings)
     if PostgreSQL refuses -> roll back to the savepoint, journal INTEGRITY_BACKSTOP
  7. store the response bytes on the key
COMMIT                                 (deferred double-entry check runs here)
```

## Phase 3: auth and the API (`app/api/`, `app/security.py`, `app/main.py`)

* Passwords: stdlib scrypt. Tokens: HS256 JWT with `exp` and a `tv` (token version).
* Public sign-up (`POST /api/auth/register` → `admin_ops.register_holder`) always creates an account holder with one ₹0 account. The role is hard-coded in the SQL, unknown fields are refused, and sign-ups are rate-limited per address.
* `deps.current_user` re-reads the user **from the database on every request**: role, `is_active`, `tv`. A demoted or deactivated user's token stops working at once, and a forged `role` claim does nothing.
* Money routes do **not** pre-filter by role. The engine decides, so every refusal lands in the journal.
* `schemas.py`: every body is `extra="forbid"` (no mass assignment). Amounts and memos get strict validators.
* `main.py`: uniform error JSON and security headers (strict CSP, no framing).

## Phase 4: Sentinel, the AI layer (`app/sentinel/`)

* `risk.py`: deterministic rules plus robust statistics give a 0–100 score with reasons. At 60+ the transfer becomes a HOLD (money to suspense). It only reads.
* `narrator.py`: an optional LLM writes words for reviewers. It never sees memos, every number it writes must be in the facts (grounding check), there is a template fallback, and results are cached by facts hash.
* `app/ledger/ai_notes.py`: calls the narrator **outside** any transaction, then journals the note.
* `engine.decide_hold`: a person approves or rejects. The decision entry stores the suggestion it saw.

## Phase 5: proof (`app/ledger/proof.py`, `tools/verify_bundle.py`)

* `run_proof()` replays everything inside one REPEATABLE READ snapshot.
* `export_bundle()` plus `tools/verify_bundle.py`: an independent offline check that trusts only the public key.

## Phase 6: the UI (`static/`)

Vanilla JS, no build step. It never uses `innerHTML` for data. Each form gets a fresh idempotency key, and retries reuse it. The **Race Lab** (holder) fires real concurrent requests. The **Proof** page draws a seal from the live chain-head hash: change any past entry and the pattern changes.

## Phase 7: tests that really race (`tests/`, `tools/`)

* `hooks.race_window(after_balance_check=0.3)` makes the dangerous interleaving happen every time. `hooks.faults(...)` crashes a request between debit and credit. One test kills the database session mid-transfer.
* Race tests assert the outcome **and** that the database backstop never fired.
* `tools/mutation_check.py` removes 37 locks and checks one by one; every removal fails a test (`docs/MUTATION_REPORT.md`).
* `tools/attack.py` runs the judges' attacks against a live URL.

---

## Run it on Windows

You need **Python 3.13** (python.org, tick "Add to PATH") and **PostgreSQL 16**, either from postgresql.org (installer) or via Docker Desktop.

```powershell
cd "C:\Abhinav's\hackathon\ledgerline"
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements-dev.txt
copy .env.example .env               # then edit DATABASE_URL / TEST_DATABASE_URL if needed
```

Database, with Docker Desktop:

```powershell
docker compose up -d db              # Postgres on localhost:5432, user/pass ledgerline, both databases created
```

Or with the PostgreSQL installer, open **SQL Shell (psql)** as `postgres` and run:

```sql
CREATE USER ledgerline WITH PASSWORD 'ledgerline';
CREATE DATABASE ledgerline OWNER ledgerline;
CREATE DATABASE ledgerline_test OWNER ledgerline;
```

Then:

```powershell
python -m app.cli migrate            # schema + signed GENESIS entry
python -m app.cli seed               # demo people and accounts (prints them)
uvicorn app.main:app --reload        # open http://localhost:8000
python -m pytest                     # 119 tests, about 20 s
python tools/attack.py               # in a second terminal, while the server runs
python tools/mutation_check.py      # about 10 minutes; proves the tests bite
```

Or everything in Docker: `docker compose up --build`, then open http://localhost:8000.

Reset the local database: `python -m app.cli reset --yes`, then migrate and seed again.

---

## Interview prep: questions you will get, and where the answers are

**"Show me exactly where the balance is checked and where the debit is written."**
Check: `engine.py`, in `transfer()`, the line `if amount > accounts.available(source):  # THE balance check` (~line 374). It runs after `accounts.lock(...)` (~line 364), which did `SELECT … FOR UPDATE` on that row.
Debit: `_commit()` calls `journal.append_entry()`. Its `INSERT INTO postings` (~journal.py line 123) fires `postings_apply`, which does `UPDATE accounts SET balance = balance + NEW.amount` (schema.sql ~line 286) on the same row. The `balance_floor` CHECK runs inside that UPDATE.

**"Are those atomic?"**
Yes. One transaction (`run()`, `with conn.transaction()`), on a row we hold `FOR UPDATE` from the check until COMMIT. Nobody else can change that balance in between, because every writer must take the same lock first.

**"What if two run at the same instant?"**
The second blocks on `SELECT … FOR UPDATE` until the first commits, then re-reads the balance and is refused. Show `test_two_withdrawals_at_the_same_instant_cannot_overdraw`, or the Race Lab.

**"And if someone removes that lock?"**
Both would pass the check. When the second posts, the database refuses it: either the floor CHECK fires (`23514`) or the stale-balance tripwire does (`LL004`). The request is refused as `INTEGRITY_BACKSTOP`, so no money is lost. But our race test asserts the backstop never fires, so the test **fails**: mutation M01 in `docs/MUTATION_REPORT.md`. Run `python tools/mutation_check.py M01` live.

**"What if the same request arrives twice?"**
The first statement of the transaction is `INSERT INTO idempotency_keys … ON CONFLICT DO NOTHING` (idempotency.py ~line 59). The second request's INSERT conflicts:
* If the first already committed, we read its stored response and return the same bytes with `Idempotent-Replayed: true`.
* If the first is still in flight, PostgreSQL makes the second INSERT wait for it. The second then replays, or proceeds as the first if the first rolled back.

Every duplicate is a row in `idempotency_replays` (Auditor → Duplicates).

**"Where does the key live, and how long?"**
Table `idempotency_keys`, primary key `(user_id, idem_key)`. Forever: expiry would be a double-payment window.

**"Why double entry?"**
External money and held money are real accounts, so conservation is one checkable rule: every entry sums to zero, hence all balances sum to zero. Holds, approvals and reversals are just more balanced entries.

**"What stops someone editing the ledger?"**
1. There is no route that edits it.
2. Triggers refuse UPDATE, DELETE and TRUNCATE.
3. If a DBA disables the triggers anyway, the hash chain breaks at that row.
4. If they also recompute every hash, the Ed25519 signatures fail, because the key is not in the database.
5. If they also have the key, only an outside anchor detects it (a saved chain head or a receipt). That is THREATS A2, an honest open risk.

**"Can the AI move money?"**
No. Sentinel only reads and scores. Above the threshold, money waits in suspense for a person. An architecture test fails if the AI package ever imports money code. The LLM writes words only: no memos in, numbers checked out.

**"Can anyone make themselves an admin from the Create user page?"**
No. `register_holder()` in `admin_ops.py` inserts the role `'holder'` as a constant, and `SignUpIn` refuses unknown fields, so sending `role` gets a 422. Admins and auditors come only from `POST /api/users`, which requires an admin. Tests: `tests/test_signup.py`; mutation M36.

**Likely live-change requests, and where to make them:**

* *"Change the overdraft maximum to ₹1 lakh."* Edit `MAX_OVERDRAFT_PAISE` in `money.py` and the `overdraft_limit BETWEEN 0 AND 5000000` CHECK in `schema.sql` (it is enforced in both places). Reset and migrate.
* *"Add a per-transaction limit of ₹50,000 for holders."* In `engine.transfer`, after the lock: `if amount > 50_000 * 100: return _refuse(ctx, attempt, "LIMIT_EXCEEDED")`, add `"LIMIT_EXCEEDED": (409, "...")` to `REFUSALS`, and add a test.
* *"Make the hold threshold 70."* `HOLD_THRESHOLD` in `sentinel/risk.py`.
* *"Let the holder cancel their own held payment."* Reuse `decide_hold(..., "reject")` with a holder check: `actor.id == hold["actor_id"]`.

## Exercises to make it yours (and real PROMPTS.md entries)

1. Delete `FOR UPDATE` in `accounts.lock()`, run `python -m pytest tests/test_concurrency.py -x`, watch it fail, and put it back.
2. Comment out the balance check in `transfer()` and run the tests. Which test catches it, and why not a race test?
3. Open Race Lab as Priya: fire 10 transfers that together exceed the balance, then look at the statement.
4. In psql, as the owner: `ALTER TABLE ledgerline.journal DISABLE TRIGGER journal_append_only;` then change a memo, run the Proof page and find the broken entry. Restore the memo and re-enable the trigger.
5. Ask an AI tool to "simplify" `engine.run()`. Check whether its version keeps the idempotency claim inside the same transaction. If not, that is a PROMPTS.md entry.
