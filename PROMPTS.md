# PROMPTS.md — every prompt we gave the AI, and where it was wrong

LedgerLine was built with an AI coding assistant (Claude), in one long build
conversation from 6 to 12 November 2026. The assistant wrote the code, ran it
against a real PostgreSQL and copied the files to our laptop. We ran them
there, and committed and pushed them ourselves. This file has two parts:

1. **[Where the AI was wrong](#part-1-where-the-ai-was-wrong)**: 14 dated
   entries in the form the problem statement asks for: what we asked for,
   what came back, what was specifically wrong with it, how we caught it, what
   we did instead, and the commit with the fix. Entries 1 and 2 are about the
   idempotency and concurrency logic.
2. **[Every prompt, in order](#part-2-every-prompt-in-order)**: all 29
   prompts we gave the AI while building LedgerLine, with what each one led
   to and the commits it went into.

**Rule for this file:** only real events. If an entry says the AI was right,
it says how that was checked. Entries are added as the work happens, not
written at the end.

---

## Part 1: where the AI was wrong

Entries 1–6 come from the first build session (6 Nov 2026). Their fixes went
into the code before the first commit, so their "fixed in" link is the commit
that first added the files. Later entries link the exact fix commit.

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
* **Fixed in:** [1abcbe5](https://github.com/maddaliabhinav/LedgerLine/commit/1abcbe558e0312f6ecb4e47cc20c509aa706b7d6)

### 13. 2026-11-09 — An attack script that would have locked itself out on its second run (demo)

* **Asked:** Add the password change to `tools/attack.py`: a stolen session trying to change the password.
* **It produced:** A first draft that also proved the throttle by making 12 wrong guesses at the current password, then had the owner change it.
* **What was wrong:** The throttle counts failures per address across all users, not just per user. The draft would have left 11 failures from the script's own address, so the owner's change (same address) got `429` instead of showing that it ends the thief's session. Worse, anyone running `attack.py` again within 5 minutes, for example a judge trying it twice, would have been stopped at the start: `cannot sign in as admin: 429`.
* **How it was caught:** Reading the draft against how `LoginThrottle` keys its counters, before running it. We then ran the corrected script twice in a row against a two-worker server: 17/17 stopped both times.
* **What we did instead:** The attack makes one wrong attempt (`403 WRONG_PASSWORD`), lets the owner change the password, and shows the thief's session is gone (`401 SESSION_REVOKED`). Throttling of guesses is proven in the test suite (`test_guessing_the_current_password_is_throttled`), where the throttle is reset between tests.
* **Fixed in:** [1abcbe5](https://github.com/maddaliabhinav/LedgerLine/commit/1abcbe558e0312f6ecb4e47cc20c509aa706b7d6)

### 14. 2026-11-13 — A commit link with an invented hash (docs)

* **Asked:** Link P20 and P21 in the prompt log below to the commits they led to, now that those commits are on GitHub.
* **It produced:** A link for `5e17e93` whose first seven characters were the real commit, followed by 33 characters it had made up instead of copying the full hash.
* **What was wrong:** GitHub looks a commit up by the hash in the link, so it would have opened a 404. A judge who clicked it would have had good reason to doubt the rest of this file, whose whole point is that every claim links to real history.
* **How it was caught:** The AI then printed the real hashes with `git rev-parse` to finish the other links, and the first one did not match what it had already written. It then checked every commit link in this file against a clone of the repository.
* **What we did instead:** Hashes are copied from git's own output, never typed. All 41 commit links in this file were checked against git and resolve to real commits.
* **Fixed in:** the commit that adds this entry, with P29.

---

## Part 2: every prompt, in order

Every prompt we gave the AI while building LedgerLine, oldest first. Dates
are India time.

* **P1–P9 are summaries.** Their exact wording was not kept, so they say
  what we asked for.
* **P10–P29 are exact**, quoted as typed, typos included. A line break is
  shown as " / ", and long error messages are shortened with "…".

Short replies inside a task, such as our answers to the AI's questions, are
listed with the prompt they belong to. Every commit with the AI's work in it
is linked from one of these prompts.

#### P1 · 6 Nov · The brief
* **Asked** (summary): We shared the problem statement and asked for a complete LedgerLine in Python, good enough to win.
* **Result:** The first version, built and checked phase by phase: the database schema that refuses to break the books; the money engine (row locks, idempotency, hash chain, signatures); sign-in, roles and the API; Sentinel; the proof and the offline verifier; the web UI with the Race Lab; tests that really race, the mutation harness and the attack script; and the documents (README, THREATS, PROMPTS, API, write-up, deployment, demo script, walkthrough). The AI ran the tests, the mutation harness and the attack script itself, and reviewed a screenshot of every page, before copying the project to our laptop.
* **Commit:** [642ca32](https://github.com/maddaliabhinav/LedgerLine/commit/642ca32dd7be2ae325955a7ad5037f9f2ed76446) (pushed on 7 Nov). What it got wrong on the way: entries 1–6.

#### P2 · 6 Nov · A broken requirements file
* **Asked** (summary): On the laptop, `pip freeze > requirements.txt` had replaced the tested versions with another environment's packages (pandas, SQLAlchemy and others). We asked the AI to sort it out.
* **Result:** It put the tested versions back and kept the frozen list next to the project folder as `requirements.freeze-backup.txt`.

#### P3 · 7 Nov · Setting up on Windows with Python 3.14
* **Asked** (summary): Get the project running on our Windows laptop, which had Python 3.14.
* **Result:** The AI checked that every pinned package has a ready-made Windows wheel for Python 3.14, ran the whole test suite on Python 3.14, and compared our copy of the project with its own, file by file.

#### P4 · 7 Nov · Putting it on GitHub
* **Asked** (summary): Push the project to our GitHub repository, which already had a README created on GitHub.
* **Result:** The first commit, then a merge with GitHub's README.
* **Commits:** [642ca32](https://github.com/maddaliabhinav/LedgerLine/commit/642ca32dd7be2ae325955a7ad5037f9f2ed76446), [524716c](https://github.com/maddaliabhinav/LedgerLine/commit/524716c34eb5127f0caffeb489966bb18ee72f6e).

#### P5 · 7 Nov · A new look, and "Create user"
* **Asked** (summary): Re-theme the UI in midnight black, pure white and vibrant golden yellow; add "Create user" on the home page; remove the demo people list.
* **Result:** The black-and-gold theme; "Sign in" and "Create user" tabs (a new account holder gets one ₹0 account); a sign-up limit per address; tests for sign-up.
* **Commit:** [dd5f527](https://github.com/maddaliabhinav/LedgerLine/commit/dd5f5278127c3ff014a9ab6c6a4187fc8efad95c). What it got wrong: entry 7.

#### P6 · 8 Nov · Choose "account holder" or "auditor" at sign-up
* **Asked** (summary): On the home page, let people choose "account holder" or "auditor" when they create a user. An auditor account must wait until an administrator accepts the request.
* **Result:** An auditor sign-up becomes a request that an administrator decides once, on the record; database guards `LL012`–`LL014`; the first schema migration; tests, mutations and attacks for the flow.
* **Commit:** [3b5290d](https://github.com/maddaliabhinav/LedgerLine/commit/3b5290da77e0bb59173a1215b27dad0a1d9eaa1c). What it got wrong: entry 8.

#### P7 · 8 Nov · A script for the presentation and the interview
* **Asked** (summary): A script to present the project to the judges and to prepare for the interview.
* **Result:** A shared document outside the repository, "LedgerLine — Project Walkthrough Script", written from the problem statement: a 12–15 minute walkthrough of every flow, and the questions the judges are likely to ask, with answers. Kept up to date afterwards (P17).

#### P8 · 9 Nov · Our logo
* **Asked** (summary): We shared our logo image and asked for it in the app.
* **Result:** The logo in the top bar and on the home page, a favicon and a home-screen icon. With it, entry 8 got the link to its fix commit.
* **Commits:** [2049fef](https://github.com/maddaliabhinav/LedgerLine/commit/2049fefa758e00b56b4f612e113585a47c6b4ab1), [2339714](https://github.com/maddaliabhinav/LedgerLine/commit/2339714039797568355654055bdd5d4fb82ea440).

#### P9 · 9 Nov · A main administrator
* **Asked** (summary): Let the first administrator create more administrators, so approvals and deposits do not wait for one person. Only that main administrator may add administrators or remove their access, nobody may remove the main administrator, and any administrator does the daily work. Update PROMPTS, THREATS and the README.
* **Result:** Migration 003 with database guards `LL013`–`LL016`; the API, UI and proof changes; 14 tests, mutations M48–M59 and an insider attack.
* **Commits:** [8220663](https://github.com/maddaliabhinav/LedgerLine/commit/82206635ffcca617f0ed5a33a455ddfbad039e95), [a1e2d82](https://github.com/maddaliabhinav/LedgerLine/commit/a1e2d820128596a1e15803f83536764448dda9b0), [a18435c](https://github.com/maddaliabhinav/LedgerLine/commit/a18435ccbcddefd8b572ad902f19e0bfbd41a448), [c8699bc](https://github.com/maddaliabhinav/LedgerLine/commit/c8699bc0652351abf12b844b63381332556de728), [a006025](https://github.com/maddaliabhinav/LedgerLine/commit/a0060255bee2ae0a2de516f1a00a31a72d918531), [0275e48](https://github.com/maddaliabhinav/LedgerLine/commit/0275e485e93c50a617d2a1b105f77bc2b4b136ae). What it got wrong: entries 9 and 10.

#### P10 · 9 Nov · The server refused to start
* **Asked** (exact, shortened): `RuntimeError: the database schema is at version 2 but this code needs version 3…`
* **Result:** Run `python -m app.cli migrate` once: it upgrades the database in place, keeps its history, and records the switch to the main-administrator rule as a signed entry.

#### P11 · 9 Nov · Committing the main administrator
* **Asked** (exact): "done / i want to push this into git"
* **Result:** Step by step: a new branch `feature/main-admin`, five commits (code, proof, UI, tests, docs), then a push.

#### P12 · 9 Nov
* **Asked** (exact, pasted output): "git checkout -b feature/main-admin / Switched to a new branch 'feature/main-admin'"
* **Result:** The next step.

#### P13 · 9 Nov
* **Asked** (exact): "all the five at a time"
* **Result:** All five commits as one block to paste.

#### P14 · 9 Nov · Personal details
* **Asked** (exact): "give the feature for the everyuser / like basic details about personal details as every bank needs"
* **Our answers to the AI's four questions:** no ID numbers; an administrator verifies the details; no change to how money moves; only the person and administrators see them.
* **Result:** Migration 004 (guard `LL017`); the details API; a "My details" page and a check queue for administrators; a 15th proof check; 12 tests, mutations M60–M70 and an attack.
* **Commits:** [49db00f](https://github.com/maddaliabhinav/LedgerLine/commit/49db00f2e1baf1cffc3f6315b28325df458117b5), [5f872d9](https://github.com/maddaliabhinav/LedgerLine/commit/5f872d97cabc0b41d06a626741c8739ff54ef83f), [106d9f5](https://github.com/maddaliabhinav/LedgerLine/commit/106d9f5381e8689861496778a6d38956b0931a1a), [d7958ea](https://github.com/maddaliabhinav/LedgerLine/commit/d7958ea6bb136f50ac6261c2d889e3da56730877), [c04e53d](https://github.com/maddaliabhinav/LedgerLine/commit/c04e53d82b0377ff281bba41b637bfb42f8b1000). What it got wrong: entry 11.

#### P15 · 9 Nov
* **Asked** (exact): a pasted `git log` showing those commits pushed.
* **Result:** Entry 11 linked to its fix commit.
* **Commit:** [788ac5e](https://github.com/maddaliabhinav/LedgerLine/commit/788ac5e19123af64473ad2a4b12bb4a5aea5c092).

#### P16 · 9 Nov · Change your own password
* **Asked** (exact): "provide a feature for all the uses to chnage password after login / like / current / new / re eneter new"
* **Result:** `POST /api/me/password` and a "Change password" page; every other session signed out on a change; migration 005 (guard `LL018`); tests, mutations M71–M76 and a stolen-session attack.
* **Commits:** [1ce8030](https://github.com/maddaliabhinav/LedgerLine/commit/1ce80306fc8178c489dd082901c7333d4ef1a715), [9d922da](https://github.com/maddaliabhinav/LedgerLine/commit/9d922da4ecf803af5b2d93ff4192995658596a92), [1abcbe5](https://github.com/maddaliabhinav/LedgerLine/commit/1abcbe558e0312f6ecb4e47cc20c509aa706b7d6), [0eba443](https://github.com/maddaliabhinav/LedgerLine/commit/0eba4434284bba8a6dd38094a7be971889842089). What it got wrong: entries 12 and 13.

#### P17 · 9 Nov · Bring the documents up to date
* **Asked** (exact): "once update the readme file,prompts,threathes and all the documents realetd till now"
* **Result:** README, THREATS, PROMPTS (links for entries 12 and 13), API, walkthrough, write-up, deployment guide, demo script and the auditor's permission wording, plus the presentation script from P7.
* **Commit:** [3ad3797](https://github.com/maddaliabhinav/LedgerLine/commit/3ad379767882e3f7cdaad0474843ec61b1131733).

#### P18 · 12 Nov · Windows blocked the server
* **Asked** (exact, shortened): `Program 'uvicorn.exe' failed to run: An Application Control policy has blocked this file.`
* **Result:** Windows was blocking `uvicorn.exe`, a small launcher that pip creates on each machine, not Python itself. Start the server with `python -m uvicorn app.main:app --reload`.

#### P19 · 12 Nov · Windows blocked the database driver
* **Asked** (exact, shortened): `ImportError: no pq wrapper available. … couldn't import psycopg 'binary' implementation: DLL load failed while importing pq: An Application Control policy has blocked this file. … couldn't import psycopg 'python' implementation: libpq library not found`
* **Result:** Use psycopg's pure-Python mode with PostgreSQL's own `libpq.dll` (`PSYCOPG_IMPL=python`, PostgreSQL's `bin` folder on `PATH`), and start every tool with `python -m` ([walkthrough](docs/WALKTHROUGH.md#if-windows-blocks-a-file)).
* **What was wrong in that answer:** it said `.venv-test/` was missing from `.gitignore`. It is there; the AI had read an out-of-date copy of the file instead of ours. Caught while preparing P20, when it compared its copies with ours before editing anything.

#### P20 · 12 Nov · Check the documents against the problem statement
* **Asked** (exact): "analyze and update the documentation according to the given problem statement / and in prompts ,mention every prompt that used to make this project"
* **Result:** The problem statement checked point by point in the README; the attacks it names mapped to our threats and tests in THREATS.md; every endpoint in the API guide's permission table; the isolation level explained in the write-up; Windows steps that avoid blocked launchers; and this part of PROMPTS.md.
* **Commit:** [5e17e93](https://github.com/maddaliabhinav/LedgerLine/commit/5e17e93d21e750cf5b8c22ac3fa3ced73f98b0a2).

#### P21 · 12 Nov · A branch of its own
* **Asked** (exact): "i have used the change password name alredy"
* **Result:** The documents go on a new branch, `feature/docs-problem-statement`, instead of the password feature's branch, and that branch is merged into `main`, so `main` finally holds all the work since 7 Nov.
* **Commits:** [5e17e93](https://github.com/maddaliabhinav/LedgerLine/commit/5e17e93d21e750cf5b8c22ac3fa3ced73f98b0a2), merged into `main` in [13edbce](https://github.com/maddaliabhinav/LedgerLine/commit/13edbcec7b785a626ac734678e5a007f1a1f19fb) and [aff52a0](https://github.com/maddaliabhinav/LedgerLine/commit/aff52a0458000b3d08eefc503ee0f7b2f00b47ed).

#### P22 · 12 Nov
* **Asked** (exact): "okay completed"
* **Result:** The AI checked GitHub: the new branch was pushed, `main` held everything since 7 Nov, and the stray web edits on `main` were gone.

#### P23 · 12 Nov · Putting it online
* **Asked** (exact): "i want to live this project / suggest me the ways to deploy the complete project"
* **Result:** Four ways compared (Render Blueprint, a Render web service with a Neon database, Railway, a server of our own with Docker), with the current free-tier limits looked up. Recommended Render with Neon, so the database outlives Render's 30-day free database.

#### P24 · 12 Nov · Vercel
* **Asked** (exact): "i have deplpoyed my project in vercel / gettin gthese errors" (the error text did not come through)
* **Result:** The AI asked for the error, and listed what LedgerLine needs on Vercel: an outside PostgreSQL (Neon, with its direct, non-pooled connection string), `APP_ENV`, `SECRET_KEY` and `LEDGER_SIGNING_KEY`, and `migrate` and `seed` run once, because Vercel runs no start command.

#### P25 · 13 Nov
* **Asked** (exact): "what are commands are used to run frontend and backed"
* **Result:** One command runs both: the FastAPI backend serves the plain HTML and JavaScript frontend itself (`python -m uvicorn app.main:app --reload`, then http://localhost:8000).

#### P26 · 13 Nov
* **Asked** (exact): "what are the tech stacks used in this project"
* **Result:** The stack, layer by layer, from `requirements.txt`, the Dockerfile and the code.

#### P27 · 13 Nov
* **Asked** (exact): "how to deploy this project online s=give simple language for live"
* **Result:** The Render Blueprint steps in plain words, with what to check afterwards.

#### P28 · 13 Nov
* **Asked** (exact): "database how to connect"
* **Result:** On Render, nothing to do. On Vercel, a Neon database and four settings, and a warning: create the tables with the same `LEDGER_SIGNING_KEY` as the live site, or its proof rejects the first entries.

#### P29 · 13 Nov · Auditors without an approval step
* **Asked** (exact): "when the user like accountant or main admin or auditor creates a new account / remove the approval for it / create account directly"
* **Our answer to the AI's question** (whether auditors should sign up instantly, which would let anyone on the internet read every account, or be created by an administrator): administrators create auditors.
* **Result:** Sign-up makes account holders only (`"role": "auditor"` is a 422); the auditor request queue, its API and the seeded waiting request are gone; an auditor an administrator creates signs in at once. Requests decided before the change stay as guarded history. `tests/test_staff_access.py` replaces the request tests; mutations M38 and M39 aimed at the new rule, M40 and M41 retired with the code they tested (74 in all); attack 12 rewritten.
* **Commits:** the ones that add this entry.
