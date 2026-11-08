# PROMPTS.md — how AI was used, and where it was wrong

LedgerLine was built with an AI coding assistant (Claude). This file records
what we asked for, what came back, what was specifically wrong with it, how
we caught it, and what we did instead. Entries are dated and added as work
happens, not written at the end.

**Rule for this file:** only real events. If an entry says the AI was right,
it says how that was checked.

Entries 1–6 come from the first build session (6 Nov 2026). Their fixes went
into the code before the first commit, so their "fixed in" link is the commit
that first added the files. Later entries link the exact fix commit.

---

### 1. 2026-11-06 — Replayed responses were not the same bytes (idempotency)

* **Asked:** Make every money-moving request idempotent. Store the result under the client's key, and have a duplicate return the first result without moving money.
* **It produced:** A sound design (the key is claimed inside the same transaction as the money movement, so duplicates wait on the key's primary key). But it stored the response in a `JSONB` column and replayed the parsed JSON.
* **What was wrong:** PostgreSQL's JSONB does not keep key order. The first response and the replays carried the same data in a different order, so "the second attempt returns the same answer" held after parsing but not byte for byte. A judge diffing two `curl` outputs would see different responses. Money still moved only once (1 journal entry, 11 replays), which is exactly why this was easy to miss.
* **How it was caught:** The Race Lab "replay storm" sends one request 12 times with the same key and checks that every body is identical. It reported *"Copies got different answers"* while also showing *1 journal entry, 11 answered from storage*.
* **What we did instead:** `idempotency_keys.response` became `TEXT` holding the exact serialized response. The first response and every replay send those same bytes (`Outcome.render()`, `routes_money.respond`). Tests now assert byte equality: `test_same_key_twice_moves_money_once_and_returns_the_same_bytes` and, over real HTTP, `test_replayed_request_over_http_returns_identical_bytes`.
* **Fixed in:** [642ca32](https://github.com/maddaliabhinav/LedgerLine/commit/642ca32dd7be2ae325955a7ad5037f9f2ed76446) (initial commit)

### 2. 2026-11-06 — Where the balance is checked and the debit is written (money integrity, verified rather than trusted)

* **Asked:** Write the withdraw/transfer path so two concurrent requests cannot both spend the same money.
* **It produced:** `engine.transfer()` locks both account rows (`SELECT … FOR UPDATE`, ascending id), checks `amount > accounts.available(source)` on the locked row, and writes the debit through `journal.append_entry()`. The posting trigger does `UPDATE accounts SET balance = balance + amount` on the same row, with a `CHECK` floor. Its claim: "check and debit are atomic, so the race is closed."
* **What was wrong:** Nothing in the logic, but the claim alone was not evidence. A confident "this is atomic" is exactly the answer that loses money when it is false, and the judges will remove a lock and re-run our tests.
* **How it was checked:**
  1. The concurrency tests force the bad interleaving (`hooks.race_window(after_balance_check=0.3)` widens the gap between check and write). They assert the outcome (one paid, one `INSUFFICIENT_FUNDS`) **and** that the database backstop never fired, so a missing lock fails a test even though PostgreSQL would still save the money.
  2. `tools/mutation_check.py` removes the defences one at a time: the row lock (M01), lock ordering (M02), the chain-head lock (M03), the balance checks (M04/M05), the floor CHECK (M17), the stale-balance tripwire (M18) and the rest. Every mutation made a test fail (see `docs/MUTATION_REPORT.md`).
  3. `tools/attack.py` was run against a server with **two worker processes**: 2×₹600 from ₹1,000 gave `[201, 409]`, and 25×₹100 against ₹400 paid exactly 4.
* **What we did instead:** Kept the code, added the mutation harness and the race windows so the claim is re-checkable by anyone: `python tools/mutation_check.py`.
* **Fixed in:** [642ca32](https://github.com/maddaliabhinav/LedgerLine/commit/642ca32dd7be2ae325955a7ad5037f9f2ed76446) (initial commit)

### 3. 2026-11-06 — Login throttle trusted a header the client controls (security)

* **Asked:** Rate-limit failed logins per client IP. The app will run behind a hosting proxy.
* **It produced:** `_client_ip()` took the **first** entry of `X-Forwarded-For`.
* **What was wrong:** The first hop is whatever the client sends. An attacker could put a new fake IP in that header on every request and never hit the limit. A proxy *appends* the real address, so only the **last** hop is trustworthy, and only when there really is a proxy in front.
* **How it was caught:** Reviewing the code while writing the deployment config (`--proxy-headers` on Render) and asking "who writes this header?"
* **What we did instead:** Use the last hop, and only when `TRUST_PROXY` is on (default only in production). Recorded as T41 in THREATS.md.
* **Fixed in:** [642ca32](https://github.com/maddaliabhinav/LedgerLine/commit/642ca32dd7be2ae325955a7ad5037f9f2ed76446) (initial commit)

### 4. 2026-11-06 — Dependency versions "from memory" that were never tested (deployment)

* **Asked:** Write `requirements.txt` for FastAPI, psycopg 3, PyJWT and cryptography.
* **It produced:** Plausible pins (`fastapi==0.115.12`, `psycopg==3.2.9`, …) that differed from the versions the code had actually been developed and tested against.
* **What was wrong:** A clean clone would have installed a different stack than the one the tests passed on. That is exactly the "works on my machine" failure that sinks a deploy on the last day.
* **How it was caught:** Comparing the pins with `pip list` in the environment where the tests ran.
* **What we did instead:** Pinned the exact tested versions, created a fresh virtualenv from `requirements-dev.txt` only, ran the full suite inside it, and added `.python-version` (3.13) so Render does not pick its newer default.
* **Fixed in:** [642ca32](https://github.com/maddaliabhinav/LedgerLine/commit/642ca32dd7be2ae325955a7ad5037f9f2ed76446) (initial commit)

### 5. 2026-11-06 — A test that encoded the wrong rule (testing)

* **Asked:** Write a test that Sentinel's risk scoring is read-only and deterministic, and that it flags a payment draining the account.
* **It produced:** A test sending ₹4,900 out of ₹5,000 and expecting the `DRAINS_ACCOUNT` signal.
* **What was wrong:** The rule (correctly) ignores amounts under ₹10,000, so the code was right and the *test* was wrong. A test written by the same tool that wrote the code can share its misunderstanding, or invent a rule the code never had.
* **How it was caught:** The test failed; reading `risk.py` instead of the test name showed the ₹10,000 minimum.
* **What we did instead:** Fixed the test to ₹19,000 out of ₹20,000. We now read the rule before trusting a red test, and run the mutation harness so we know the tests fail for the right reason.
* **Fixed in:** [642ca32](https://github.com/maddaliabhinav/LedgerLine/commit/642ca32dd7be2ae325955a7ad5037f9f2ed76446) (initial commit)

### 6. 2026-11-06 — UI bugs visible only on screen (frontend)

* **Asked:** Build the single-page UI with a small DOM helper and a hash-chain "journal" view.
* **It produced:** Working pages, with two defects. A CSS class `.link` was used both for journal rows (a CSS grid) and for link-style buttons (`.btn.link`), so every "View receipt" button became a 3-line grid. And `element.replaceChildren(a, b, null)` printed the word **"null"**, because the DOM converts `null` to text.
* **What was wrong:** Name collisions and DOM API semantics. Neither shows up in an API test.
* **How it was caught:** Screenshots of every page (Playwright) reviewed before shipping.
* **What we did instead:** Renamed the journal rows to `.chain-entry` and routed every child replacement through a null-safe `set()` helper.
* **Fixed in:** [642ca32](https://github.com/maddaliabhinav/LedgerLine/commit/642ca32dd7be2ae325955a7ad5037f9f2ed76446) (initial commit)

### 7. 2026-11-07 — Gold on every row made gold meaningless (ui)

* **Asked:** Re-theme the UI in midnight black, pure white and vibrant golden yellow; add "Create user" on the home page; remove the demo people list.
* **It produced:** A working black-and-gold theme, but it styled every "Posted" status chip in gold. A statement became a column of gold badges, so gold no longer meant "needs attention" (held payments) or "act here" (buttons).
* **What was wrong:** The one colour meant to draw the eye was spent on the most common, least interesting state.
* **How it was caught:** Screenshot review of the statement page after the change.
* **What we did instead:** "Posted" is now a quiet white outline. Gold is kept for buttons, held payments, the active tab and the proof seal; red only for refusals and errors.
* **Fixed in:** [dd5f527](https://github.com/maddaliabhinav/LedgerLine/commit/dd5f5278127c3ff014a9ab6c6a4187fc8efad95c)

### 8. 2026-11-08 — A schema change that would have broken every existing install (deploy)

* **Asked:** On the home page, let people choose "account holder" or "auditor" when they create a user. An auditor account must wait until an administrator accepts the request.
* **It produced:** A sound design: a separate `access_requests` table (a request is not a user), a locked, decide-once approval journaled as `ACCESS_DECISION`, and database guards for each rule (`LL012`–`LL014`). It also shipped a migration, `app/migrations/002_access_requests.sql`. But the server would still start happily on a database that had not been migrated.
* **What was wrong:** Holder sign-up, admin user creation and sign-in with an unknown username now read `access_requests`. On a database created by the previous version, for example the one on our own laptop, all three would have failed with a 500 (`relation "access_requests" does not exist`) until someone thought to run `migrate` again. Nothing would have said so.
* **How it was caught:** Before delivering, we asked "what happens to the database that already exists?" We then built a version-1 database with the previous code and ran the new code's sign-up, user creation and sign-in paths against it. All three raised `UndefinedTable`.
* **What we did instead:** `migrate` now upgrades an existing database in place (it applies only the migrations it lacks, in one transaction, and is safe to re-run). The server checks the schema version at startup and refuses to start on an old one, with the fix in the message: `Run: python -m app.cli migrate`. Checked both ways: refused on version 1, then upgraded, started, and all 14 proof checks passed over the old history.
* **Fixed in:** _add link after pushing_

---

## Your entries from here

Add an entry each time you use an AI tool this week: deploying, reviewing a
function, writing the demo, fixing a bug a judge might find. Link the commit.
Template:

```
### N. YYYY-MM-DD — <short title> (<area: integrity / concurrency / idempotency / auth / deploy / ui / docs>)
* **Asked:** <the prompt, in one or two sentences>
* **It produced:** <what came back>
* **What was wrong:** <specific: which line, which case, what would have happened>
* **How it was caught:** <test, review, mutation, manual attack, screenshot>
* **What we did instead:** <the change>
* **Fixed in:** <https://github.com/<you>/<repo>/commit/<sha>>
```
