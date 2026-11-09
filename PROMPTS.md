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
* **Fixed in:** [3b5290d](https://github.com/maddaliabhinav/LedgerLine/commit/3b5290da77e0bb59173a1215b27dad0a1d9eaa1c)

### 9. 2026-11-09 — A mutation that kept testing a rule nothing runs any more (testing)

* **Asked:** Let the first administrator create more administrators, so approvals and deposits do not wait for one person. Only that main administrator may add administrators or remove their access, and nobody may remove the main administrator.
* **It produced:** Migration `003_main_admin.sql`, which redefines the `users_guard` trigger function to add the main-administrator rules, plus the API, UI and proof changes and their tests. It left `tools/mutation_check.py` as it was.
* **What was wrong:** Mutation M45 ("make user roles editable") deletes the role check from `users_guard` in migration **002**. Once 003 replaces that function, 002's copy never runs, so the mutation deleted nothing that runs. The role guard itself still worked, but our evidence that a test would notice its removal was gone, and `docs/MUTATION_REPORT.md` still said 47/47.
* **How it was caught:** Before trusting the old report, we re-ran the mutation aimed at the function the migration replaces, and M45 came back `SURVIVED`. (M28 broke too, but loudly: a line it edits in `deps.py` had changed, and the harness refuses to run a mutation whose pattern is missing. A pattern that still exists in replaced code gives no such warning.)
* **What we did instead:** M45 now edits the live copy in migration 003, with a comment saying why. We added M48–M59 for the new rules and re-ran the whole harness: 59/59 caught (`docs/MUTATION_REPORT.md`). Our rule now: when a migration replaces a database function, its mutations move with it.
* **Fixed in:** [c8699bc](https://github.com/maddaliabhinav/LedgerLine/commit/c8699bc0652351abf12b844b63381332556de728)

### 10. 2026-11-09 — The proof judged old history by a rule that did not exist yet (integrity)

* **Asked:** The same request as entry 9.
* **It produced:** A stricter access check in the proof: every administrator must have been created by the main administrator, or be the main administrator itself, created from the server's command line. Every test passed, because every test database is created fresh.
* **What was wrong:** Up to version 2, any administrator could create another one. After an upgrade, an administrator that a second administrator had created last week was suddenly reported as a fault (`administrator chitra was not created by the main administrator`). If the first administrator's access had been removed earlier, the main role went to the first *active* administrator, and both of them were reported. The proof would have shown FAIL on honest data on the day of the upgrade, and the obvious "fix" would have been to weaken the check.
* **How it was caught:** We asked the question from entry 8 again: what happens to a database that already exists? We built two version-2 databases with the previous release's code (one where a second administrator created a third, one where the first administrator's access had been removed), upgraded them with the new code and ran `python -m app.cli verify`. Both failed the access check.
* **What we did instead:** The switch to the new rule is now on the signed record. When `migrate` upgrades a database across version 3, it appends one `MAIN_ADMIN` journal entry naming the main administrator (the database allows only one). The proof judges each administrator by the rule in force when it was created: before that entry, another administrator was enough; after it, only the main administrator. A database created at version 3 or later follows the new rule from its first entry. The proof also checks that the main-administrator flag in `users` matches the signed record, so a flag moved behind the guards is caught directly. Re-checked by hand: both test databases and our UI database (all built by the old code) upgrade, pass all 14 checks, keep the new rules (the third administrator can no longer create administrators) and verify offline with `tools/verify_bundle.py`. The rule itself has tests (`test_administrators_are_judged_by_the_rule_in_force_when_they_were_created` and two more); the upgrade run is not in the automated suite, because it needs a second database built by old code.
* **Fixed in:** [a1e2d82](https://github.com/maddaliabhinav/LedgerLine/commit/a1e2d820128596a1e15803f83536764448dda9b0)

### 11. 2026-11-09 — A page the auditor was allowed to see but could not reach (ui)

* **Asked:** Add the basic personal details a bank needs for every user. We chose: no ID numbers, an administrator verifies them, money rules unchanged, and only the person and administrators see the details (auditors see only whether they are verified).
* **It produced:** The details, the checks and a "Personal details" status column on the People page, which auditors may open (the API and the page's own route both allow it).
* **What was wrong:** The auditor's menu had never linked to the People page, since the first build. The README has said since 8 Nov that auditors "see" auditor requests, which are on that page too. Both claims were true only for someone who typed `#/users` into the address bar. Every API test passed, because the API was right.
* **How it was caught:** The browser walkthrough for the new feature signed in as the auditor and checked that the menu has a way to the People page. It did not.
* **What we did instead:** Added Users to the auditor's menu, and checked the auditor's view in screenshots on desktop and phone: statuses shown, no details, no buttons.
* **Fixed in:** [106d9f5](https://github.com/maddaliabhinav/LedgerLine/commit/106d9f5381e8689861496778a6d38956b0931a1a)

### 12. 2026-11-09 — A mutation that crashed the code instead of removing the check (testing)

* **Asked:** Let every user change their own password: current password, new password, new password again. (This entry is about a mutation written in the first build, found while doing it.)
* **It produced:** The password change reuses the sign-in throttle, so the throttle's message changed. That moved the line mutation M31 edits, and the harness refused to run M31 ("pattern not found").
* **What was wrong:** Reading M31 to retarget it showed what it had been doing since the first build. Its title is "Stop throttling password guesses", but it replaced `raise TooManyRequests(...)` with `print("throttle off", ...)`, and the `headers=` argument on the next line made that `print` call fail with a `TypeError`. So the mutated server answered `500`, not "the request goes through", and the test failed because of the crash. A crash is "caught" by any test that merely passes through that line, even one that never checks throttling, while a throttle that is really switched off is caught only by a test that checks it. Here the right test happened to catch it either way, but the report could not tell the two apart.
* **How it was caught:** By reading what the replacement text actually does when it runs, not just its title.
* **What we did instead:** M31 now replaces the throttle's condition with `if False:`, so the throttle really is off and every guess goes through. `test_password_guessing_is_throttled` still fails, now for the right reason. The new password mutations (M71–M76) follow the same rule: switch the check off, never break the code around it.
* **Fixed in:** _(commit link to be added after the push)_

### 13. 2026-11-09 — An attack script that would have locked itself out on its second run (demo)

* **Asked:** Add the password change to `tools/attack.py`: a stolen session trying to change the password.
* **It produced:** A first draft that also proved the throttle by making 12 wrong guesses at the current password, then had the owner change it.
* **What was wrong:** The throttle counts failures per address across all users, not just per user. The draft would have left 11 failures from the script's own address, so the owner's change (same address) got `429` instead of showing that it ends the thief's session. Worse, anyone running `attack.py` again within 5 minutes, for example a judge trying it twice, would have been stopped at the start: `cannot sign in as admin: 429`.
* **How it was caught:** Reading the draft against how `LoginThrottle` keys its counters, before running it. We then ran the corrected script twice in a row against a two-worker server: 17/17 stopped both times.
* **What we did instead:** The attack makes one wrong attempt (`403 WRONG_PASSWORD`), lets the owner change the password, and shows the thief's session is gone (`401 SESSION_REVOKED`). Throttling of guesses is proven in the test suite (`test_guessing_the_current_password_is_throttled`), where the throttle is reset between tests.
* **Fixed in:** _(commit link to be added after the push)_

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
