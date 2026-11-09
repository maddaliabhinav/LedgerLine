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

Version 2 (`app/migrations/002_access_requests.sql`) adds the rules for **who may sign in**:

| Rule | Where | What it means |
|---|---|---|
| Only auditor access can be requested | `access_requests.role CHECK (role = 'auditor')` | Nobody can even *ask* for admin. |
| One waiting request per name | `access_requests_one_pending` (unique index) | A waiting request reserves its username. |
| Decided once, by an admin, on the record | `access_requests_guard` (`LL012`) | A request starts pending; the only allowed change is a decision whose `ACCESS_DECISION` journal entry was written by an administrator; after that it never changes. |
| Roles never change | `users_guard` (`LL013`) | `UPDATE users SET role = 'admin'` is refused. A new role means a new user, on the record. |
| Every user on the record | `users_created_on_record`, deferred to COMMIT (`LL014`) | Each new user needs a `USER_CREATED` entry with the same role, in the same transaction; an auditor or admin only from another administrator (or the server's own command line, for the seed). |

Version 3 (`app/migrations/003_main_admin.sql`) adds **the main administrator**: any administrator does the daily work, but only the main administrator decides who is an administrator.

| Rule | Where | What it means |
|---|---|---|
| One main administrator, set once | `users.is_main_admin`, unique index `users_one_main_admin`, CHECK `main_admin_is_an_admin`, `users_guard` (`LL013`) | Only the first administrator (from the command line) gets the flag; it can never move. |
| The main administrator stays | `users_guard` (`LL015`) | `UPDATE users SET is_active = false` on the main administrator is refused, whoever runs it. |
| Administrators only from the main administrator | `users_created_on_record` v3 (`LL014`) | A new administrator's `USER_CREATED` entry must be written by the main administrator. |
| An administrator's access, only by the main administrator | `users_admin_access`, deferred to COMMIT (`LL016`) | Removing or restoring an administrator needs a signed `USER_STATUS` entry by the main administrator, for that user and that state. |
| The switch is on the record | `journal_one_main_admin_switch` (unique index) + `migrate` | Upgrading an existing database appends one signed `MAIN_ADMIN` entry naming the main administrator. Administrators created before it were made under the old rule, and the proof judges them by it. |

Version 4 (`app/migrations/004_personal_details.sql`) adds **personal details**: date of birth, mobile, email, address, occupation and an optional nominee, kept in `personal_details`, never in the journal.

| Rule | Where | What it means |
|---|---|---|
| Valid formats | CHECKs on `personal_details` | 10-digit mobile starting 6–9, 6-digit PIN code, an email shape, a nominee given in full or not at all. |
| Nobody decides their own | CHECK `nobody_decides_their_own` | The decider is never the person. |
| A change is checked again | `personal_details_guard` (`LL017`) | Any change is the next version, waiting; a decision is made once per version; nothing else may change. |
| On the record | `personal_details_on_record`, deferred to COMMIT (`LL017`) | A new version needs a `DETAILS_SUBMITTED` entry by the person; a decision needs `DETAILS_VERIFIED` / `DETAILS_RETURNED` by another administrator, for that version. The entries hold no values. |
| Never deleted | `personal_details_no_delete` (`LL010`) | Corrected by a new version, not removed. |

Version 5 (`app/migrations/005_password_changes.sql`) puts **password changes** on the record:

| Rule | Where | What it means |
|---|---|---|
| A new password is on the record, by its owner | `users_password_on_record`, deferred to COMMIT (`LL018`) | A new `password_hash` needs a `PASSWORD_CHANGED` entry written by that same user in the same transaction. Nobody changes someone else's password through the app, and no change is silent. |

`python -m app.cli migrate` applies them to a new database, and upgrades an existing one in place.

System accounts: `SYS-EXTERNAL` (the outside world; the only account allowed below zero) and `SYS-SUSPENSE` (payments held for review).

## Phase 2: the money engine (`app/ledger/`)

* `engine.py`: **every rupee moves through here.** `run()` is the envelope (transaction + idempotency). `deposit`, `withdraw`, `transfer`, `decide_hold` and `reverse` are the operations.
* `journal.py`: `append_entry()`, the **only** writer of journal rows and postings. It locks the chain head, computes each posting's expected balance, hashes, signs, inserts and advances the head.
* `accounts.py`: `lock()` takes `SELECT … FOR UPDATE` on customer accounts **in ascending id order**.
* `idempotency.py`: claim (INSERT first), complete (store the exact response), answer duplicates.
* `proof.py`: replays the whole journal and runs 15 checks. `statements.py`: statements. `admin_ops.py`: open, freeze, overdraft, users (administrators only from the main administrator), auditor requests (`request_auditor_access`, `decide_access_request`) and `change_password`. `details.py`: personal details (`submit` by the person, `decide` by another administrator). `ai_notes.py`: puts AI output on the record.
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

* Passwords: stdlib scrypt. Tokens: HS256 JWT with `exp` and a `tv` (token version). `POST /api/me/password` changes your own password (current one required, wrong guesses throttled like sign-in) and moves `tv` on, which ends every other session.
* Public sign-up (`POST /api/auth/register`) offers two account types. **Holder** → `admin_ops.register_holder`: an account holder with one ₹0 account, signed in at once (the role is hard-coded in the SQL). **Auditor** → `admin_ops.request_auditor_access`: a row in `access_requests`, not a user, so there is nothing to sign in with (`403 APPROVAL_PENDING`). `role: "admin"` and unknown fields get a 422, and sign-ups are rate-limited per address.
* An admin decides on the Users page (`POST /api/access-requests/{id}/approve|decline` → `admin_ops.decide_access_request`): lock the request row (`FOR UPDATE`), check it is still pending, journal `ACCESS_DECISION`, and on approval create the auditor from the stored request plus a `USER_CREATED` entry pointing back at the decision. A second decision gets `409 ALREADY_DECIDED`.
* Usernames are unique across users **and** waiting requests: `_claim_username` takes one per-name advisory lock on every path that hands out a name, then checks both tables.
* `deps.current_user` re-reads the user **from the database on every request**: role, `is_active`, `tv`. A demoted or deactivated user's token stops working at once, and a forged `role` claim does nothing.
* Money routes do **not** pre-filter by role. The engine decides, so every refusal lands in the journal.
* `routes_details.py`: personal details. `GET/PUT /api/me/details` for your own; `GET /api/users/{u}/details` and verify / send back for administrators only (auditors see just the status in `GET /api/users`).
* `schemas.py`: every body is `extra="forbid"` (no mass assignment). Amounts, memos and personal details get strict validators (ASCII digits only, Indian mobile numbers and PIN codes, no control or direction-changing characters).
* `main.py`: uniform error JSON and security headers (strict CSP, no framing).

## Phase 4: Sentinel, the AI layer (`app/sentinel/`)

* `risk.py`: deterministic rules plus robust statistics give a 0–100 score with reasons. At 60+ the transfer becomes a HOLD (money to suspense). It only reads.
* `narrator.py`: an optional LLM writes words for reviewers. It never sees memos, every number it writes must be in the facts (grounding check), there is a template fallback, and results are cached by facts hash.
* `app/ledger/ai_notes.py`: calls the narrator **outside** any transaction, then journals the note.
* `engine.decide_hold`: a person approves or rejects. The decision entry stores the suggestion it saw.

## Phase 5: proof (`app/ledger/proof.py`, `tools/verify_bundle.py`)

* `run_proof()` replays everything inside one REPEATABLE READ snapshot: money (chain, signatures, double entry, balances, floors, conservation, suspense, decisions, exactly-once, refusals), access (every user's signed creation, the main administrator, the rule in force when each administrator was made) and personal details (who changed and who checked each version).
* `export_bundle()` plus `tools/verify_bundle.py`: an independent offline check that trusts only the public key.

## Phase 6: the UI (`static/`)

Vanilla JS, no build step. It never uses `innerHTML` for data. Each form gets a fresh idempotency key, and retries reuse it. The **Race Lab** (holder) fires real concurrent requests. The **Proof** page draws a seal from the live chain-head hash: change any past entry and the pattern changes. **Users** (People) shows roles, access, auditor requests and the personal-details queue; buttons appear only where the server would allow the action. **My details** and **Change password** are open to every role. On every page load the UI asks the server who you are (`/api/me`), so a stale saved session never shows the wrong buttons.

## Phase 7: tests that really race (`tests/`, `tools/`)

* `hooks.race_window(after_balance_check=0.3)` makes the dangerous interleaving happen every time. `hooks.faults(...)` crashes a request between debit and credit. One test kills the database session mid-transfer.
* Race tests assert the outcome **and** that the database backstop never fired.
* `tools/mutation_check.py` removes 76 locks and checks one by one; every removal fails a test (`docs/MUTATION_REPORT.md`). A mutation switches a check off; it never just breaks the code around it (PROMPTS.md entry 12).
* `tools/attack.py` runs 17 of the judges' attacks against a live URL, and can be run twice in a row (PROMPTS.md entry 13).

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
python -m app.cli migrate            # schema + signed GENESIS entry (on an existing database: upgrades it)
python -m app.cli seed               # demo people and accounts (prints them)
uvicorn app.main:app --reload        # open http://localhost:8000
python -m pytest                     # 166 tests, about 30 s
python tools/attack.py               # in a second terminal, while the server runs
python tools/mutation_check.py      # about 22 minutes; proves the tests bite
```

Or everything in Docker: `docker compose up --build`, then open http://localhost:8000.

After pulling an update, stop the server and run `python -m app.cli migrate` again: it upgrades your database in place and keeps its history (the server refuses to start until you do).

Reset the local database: `python -m app.cli reset --yes`, then migrate and seed again. Only a freshly seeded database has the sample personal details for Priya (verified) and Rahul (waiting).

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
No. `SignUpIn.role` accepts only `holder` or `auditor`, so `admin` gets a 422. A holder sign-up inserts the role `'holder'` as a constant. An auditor sign-up creates a *request*, and only an administrator can turn it into a user. Underneath, PostgreSQL refuses a request for admin (`CHECK`) and, at commit, any auditor not created on the record by another administrator and any administrator not created by the main administrator (`LL014`). Tests: `tests/test_signup.py`, `tests/test_access_requests.py`, `tests/test_main_admin.py`; mutations M36, M42, M46, M53.

**"What if two admins approve the same request at the same moment?"**
`decide_access_request` locks the request row with `SELECT … FOR UPDATE` before checking it is pending. The second admin waits for the first to commit, then sees `approved` and gets `409 ALREADY_DECIDED`: one auditor, one decision entry. The test forces this with `hooks.race_window(after_request_check=0.3)`; delete `FOR UPDATE` and it fails (M40). Even past the app, the database keeps decisions final (`LL012`, M44).

**"Could an auditor approve someone, or approve themselves?"**
The endpoint needs the admin role, re-read from the database on every request. A pending requester has no account at all, so there is nothing to approve *with*. And the database refuses any decision whose journal entry was not written by an administrator (`LL012`, M38 and M43).

**"Why a main administrator? Can a second admin take over?"**
Approvals should not wait for one person, so every administrator can deposit, approve, reverse and decide auditor requests. Deciding *who is an administrator* is different: it is how an insider would build a backdoor. So only the main administrator creates administrators or removes their access (`403 MAIN_ADMIN_ONLY` for anyone else), and nobody can remove the main administrator (`403 MAIN_ADMIN_PROTECTED`). PostgreSQL enforces the same (`LL013`–`LL016`), and the proof compares the flag with the signed record. Tests: `tests/test_main_admin.py`; attack 14 in `tools/attack.py`; mutations M48–M59.

**"What happens to an existing database when you upgrade?"**
`migrate` makes the first *active* administrator the main one and appends a signed `MAIN_ADMIN` entry saying so. Administrators that another administrator created before that entry were legal at the time, so the proof judges each one by the rule in force when it was created. Without that, the proof would have failed on honest history: PROMPTS.md entry 10.

**"Why aren't the personal details in your tamper-proof journal?"**
Because the journal is permanent and signed, and personal details must stay correctable. Anything written there can never be fixed or removed, and it ends up in every evidence export. So the details live in their own table, and the journal records only who changed or checked which version, and which fields changed. The "send back" note stays out too, because it may quote the details. Tests: `tests/test_personal_details.py`; mutation M68 writes the values into the journal and a test fails.

**"What stops an admin from verifying their own details, or verifying and then changing them?"**
`details.decide` refuses your own details (`403 OWN_DETAILS`), and the database refuses a decider who is the person (CHECK) or a decision not journaled by another administrator (`LL017`). A decision names the version the admin saw (`409 DETAILS_CHANGED` otherwise), and any change after that is a new version that waits again: the guard refuses a change that keeps "verified", even with a forged decision entry in the same transaction (M64).

**"What happens when someone changes their password?"**
`admin_ops.change_password` locks the user row, checks the current password with scrypt, refuses the same password again, writes the new hash and moves the user's `token_version` on, all in one transaction with a `PASSWORD_CHANGED` entry (who and when, nothing secret). Every other token fails its next request (`SESSION_REVOKED`), because `deps.current_user` compares the token's version with the database on every request; the response carries a fresh token for the device that made the change. Wrong current passwords count against the sign-in throttle. Tests: `tests/test_password.py`; mutations M71–M76.

**Likely live-change requests, and where to make them:**

* *"Change the overdraft maximum to ₹1 lakh."* Edit `MAX_OVERDRAFT_PAISE` in `money.py` and the `overdraft_limit BETWEEN 0 AND 5000000` CHECK in `schema.sql` (it is enforced in both places). Reset and migrate.
* *"Add a per-transaction limit of ₹50,000 for holders."* In `engine.transfer`, after the lock: `if amount > 50_000 * 100: return _refuse(ctx, attempt, "LIMIT_EXCEEDED")`, add `"LIMIT_EXCEEDED": (409, "...")` to `REFUSALS`, and add a test.
* *"Make the hold threshold 70."* `HOLD_THRESHOLD` in `sentinel/risk.py`.
* *"Let the holder cancel their own held payment."* Reuse `decide_hold(..., "reject")` with a holder check: `actor.id == hold["actor_id"]`.
* *"Let a second administrator also create administrators."* Don't do it quietly: it is a rule in three places. `admin_ops.create_user` (`MAIN_ADMIN_ONLY`), `users_created_on_record` in a new migration (`LL014`) and the proof's `admin_creation_problem`. Record the switch the way `migrate` records `MAIN_ADMIN`, so old history is judged by the old rule.

## Exercises to make it yours (and real PROMPTS.md entries)

1. Delete `FOR UPDATE` in `accounts.lock()`, run `python -m pytest tests/test_concurrency.py -x`, watch it fail, and put it back.
2. Comment out the balance check in `transfer()` and run the tests. Which test catches it, and why not a race test?
3. Open Race Lab as Priya: fire 10 transfers that together exceed the balance, then look at the statement.
4. In psql, as the owner: `ALTER TABLE ledgerline.journal DISABLE TRIGGER journal_append_only;` then change a memo, run the Proof page and find the broken entry. Restore the memo and re-enable the trigger.
5. Ask an AI tool to "simplify" `engine.run()`. Check whether its version keeps the idempotency claim inside the same transaction. If not, that is a PROMPTS.md entry.
