# LedgerLine: write-up

## What we built

A money-movement system for a company's internal accounts. Holders withdraw and transfer, administrators record deposits and decide flagged payments, and auditors read and verify everything. Three things are guaranteed:

1. **The books always balance.** Every movement is double entry: one journal entry whose postings sum to zero. The sum of every balance in the system, including the "outside world" account, is always exactly 0. No customer balance goes below its floor (₹0, or an authorised overdraft of up to ₹50,000).
2. **Exactly once.** Every money-moving request carries an Idempotency-Key. The same request arriving twice, or twenty times at once, moves money once and gets the same bytes back. Every duplicate is logged.
3. **Provable history.** The journal records every posting, every refused attempt, every AI suggestion and every human decision. It is append-only (the database refuses edits), hash-chained (each entry commits to all before it) and Ed25519-signed with a key that is not in the database. Anyone can verify an exported ledger offline with only the public key.

Alongside the system: a web UI for all three roles, including a **Race Lab** that fires real concurrent requests from the browser; **Sentinel**, an AI review layer that flags and explains but never decides; a red-team script (`tools/attack.py`); and a **mutation harness** (`tools/mutation_check.py`) that removes each of 35 locks and checks in turn and proves the test suite fails every time.

## The integrity and concurrency model, in our own words

**Where the balance is checked and where the debit is written.** In `app/ledger/engine.py`, `transfer()` first locks the sender's and receiver's account rows with `SELECT … FOR UPDATE`, lower account id first, so A→B and B→A cannot deadlock. While holding those locks it checks status, the daily limit and `amount > balance + overdraft`. The debit is written by `journal.append_entry()`, which inserts a posting. A database trigger on that insert runs `UPDATE accounts SET balance = balance + amount` on the same locked row, and the `balance_floor` CHECK constraint is evaluated by that very UPDATE. All of it, together with the idempotency key and the stored response, is one transaction: it commits whole or not at all.

**What happens when two run at the same instant.** The second request blocks on the row lock until the first commits. Then it reads the new balance and is refused (`INSUFFICIENT_FUNDS`, recorded on the statement). It never sees the old balance.

**Why we did not stop at "the app checks it".** PostgreSQL enforces the same rules again. The floor is a CHECK. Balances can only change through a posting (direct UPDATEs raise `LL002`). Every posting states the balance the app expects, and the database refuses it if reality differs (`LL004`: proof the app decided on a stale read). Postings must sum to zero at commit (`LL007`) and can only be written in the same transaction as their entry (`LL008`). If any of these ever fires, the request is refused and recorded as `INTEGRITY_BACKSTOP`, which the auditor sees as a bug alarm. Our tests assert it never fires, so a missing lock still fails a test even though the database saved the money.

**Ordering and the hash chain.** Every append takes one more lock last: the single `ledger_head` row. That gives every entry a gapless sequence number and a single global order, and lets each entry's hash include the previous hash. It costs throughput (see below). We accepted that.

**Idempotency.** The key row is INSERTed as the first statement of the money transaction. A concurrent duplicate's INSERT waits on the primary key until the first transaction finishes, then it reads and replays the stored response. A crash rolls the key back with everything else, so a retry is a fresh attempt. Keys are per user, never expire, and are bound to their first outcome (refusals included). Reusing a key for a different request returns 409.

**Double entry rather than single entry.** With external money as a real account (`SYS-EXTERNAL`) and held money as another (`SYS-SUSPENSE`), "money is conserved" becomes one simple, checkable statement: every entry sums to zero. Holds, reversals and approvals need no special cases; they are just more balanced entries.

## Where AI fits in the product, and where it must not decide

**Sentinel** scores every outgoing payment with transparent rules and robust statistics: amount against the account's own median, draining the account, new payee, velocity, structuring just under ₹1 lakh, recent refused attempts, fresh destination, pass-through money, odd hours. Each reason carries points and plain-English evidence. At 60 or above the payment is **held**: the money leaves the sender into suspense (so it cannot be spent twice meanwhile) and waits for an administrator. The suggestion is stored on the HOLD entry. The decision is a separate entry naming the person, their note, and the suggestion they saw.

An optional LLM (Claude Haiku) writes a reviewer note or a reconciliation summary. It is used only where words help a human, and fenced in four ways:

* It is never on the payment path, and never called while a database lock or transaction is open.
* It sees structured facts only. Memos are never sent, so a memo cannot inject instructions.
* Every number in its text must appear in the facts. Otherwise the text is discarded, a fixed template is shown, and the rejection is recorded.
* Results are cached by a hash of the facts, so the same question never costs a second call.

Without an API key everything works with the template.

A test enforces the architecture: the `app/sentinel` package cannot import the engine, the journal writer or admin operations, and scoring works inside a READ ONLY transaction.

## How we used AI to build it, and where it was wrong

We built LedgerLine with an AI coding assistant and treated its output as a pull request from a confident junior engineer. Every claim about locking or idempotency was checked by a test that forces the race, and every test was checked by mutation. PROMPTS.md has the specific misses: replayed responses that were equal but not byte-identical (JSONB reorders keys), a login throttle that trusted a client-controlled header, dependency pins from memory, a test that encoded a rule the code never had, and two UI defects found only by screenshot.

## What we deliberately did not build

* **Multiple currencies.** Each currency needs its own conservation equation plus FX accounts. INR only, enforced by a CHECK.
* **Dual control (4-eyes) on reversals and approvals.** Single admin plus a journal entry plus auditor flags. The first thing we would add (THREATS A1).
* **Scheduled and batch transfers.** A batch is many transfers. We would add it as one atomic entry with N+1 postings and one key, but not before the core was proven.
* **Natural-language queries over the ledger.** Letting a model write SQL against the journal is an injection vector. If ever, only via a validated filter schema.
* **A trained ML fraud model.** A reviewer must see why something was held, and an unexplainable score cannot be audited.
* **Interest, fees, statements as PDF.**

## What is unfinished, or knowingly open

* A malicious administrator can still reverse or approve wrongly. It is recorded and flagged, not prevented (A1).
* Someone holding both the database and the signing key can rewrite history consistently. We publish the chain head and give signed receipts as anchors, but do not push the head to an external timestamp service (A2/A3).
* Login throttling is in memory per process (A4). There is no rate limit on money endpoints (A5).
* No signing-key rotation procedure (A9). Held payments never auto-expire (A7).
* Throughput is bounded by the single chain head. We expect hundreds of entries per second, which we judged right for corporate payroll and vendor payments (A8).
