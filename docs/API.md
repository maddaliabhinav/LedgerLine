# LedgerLine API

Base URL: your deployment (for example `https://ledgerline.onrender.com`). Interactive docs: `/docs`.

* **Auth:** `POST /api/auth/login` returns a bearer token. Send `Authorization: Bearer <token>` on every other call. The server re-reads your role from the database on every request; the role inside the token is ignored.
* **Money:** amounts are rupees, sent as a **string** with at most 2 decimals (`"1500"`, `"1500.50"`). A JSON integer is accepted as whole rupees. Floats, booleans, negatives, zero, exponents and non-ASCII digits are refused (422). Responses carry money as `{"paise": 150050, "rupees": "1500.50", "display": "Rs 1,500.50"}`.
* **Idempotency:** every request that can move money **must** send `Idempotency-Key: <8–100 chars of A-Z a-z 0-9 _ . : ->`. The same key with the same request returns the first response byte for byte, plus the header `Idempotent-Replayed: true`. The same key with a different request returns `409 IDEMPOTENCY_KEY_REUSED`. If the first request is still running, the retry waits up to 5 s for its answer; after that it gets `409 REQUEST_IN_PROGRESS` and nothing moves. Keys are per user and never expire.
* **Refusals are recorded.** A well-formed money request that names an existing account and is refused for any business reason (not yours, no funds, frozen, wrong role…) is written to the journal and shown on that account's statement. Malformed requests (422) never reach the money logic and are not journaled.
* **Errors** always look like `{"error": {"code": "INSUFFICIENT_FUNDS", "message": "…"}}`. A refused money request also returns `status: "refused"` and a signed `receipt` for the refusal entry.

## Who can do what

| Endpoint | admin | holder | auditor | Effect on money |
|---|:-:|:-:|:-:|---|
| `POST /api/deposits` | ✓ any active customer account | – | – | `SYS-EXTERNAL` → account |
| `POST /api/withdrawals` | – | ✓ own accounts | – | account → `SYS-EXTERNAL` (or → `SYS-SUSPENSE` if held) |
| `POST /api/transfers` | – | ✓ from own accounts | – | source → destination (or → `SYS-SUSPENSE` if held) |
| `POST /api/reviews/{seq}/approve` | ✓ | – | – | `SYS-SUSPENSE` → intended destination |
| `POST /api/reviews/{seq}/reject` | ✓ | – | – | `SYS-SUSPENSE` → back to the sender |
| `POST /api/reversals` | ✓ (reason required) | – | – | new compensating entry, opposite direction |
| `POST /api/accounts` | ✓ | – | – | none (opens at ₹0) |
| `POST /api/accounts/{no}/freeze`, `/unfreeze`, `/close` | ✓ | – | – | none (stops movement) |
| `POST /api/accounts/{no}/overdraft` | ✓ | – | – | none (moves the floor to −limit, max ₹50,000) |
| `POST /api/users`, `/api/users/{u}/deactivate`, `/activate` | ✓ holders and auditors; **administrators: main admin only** | – | – | none |
| `GET /api/me/details`, `PUT /api/me/details` (your own personal details) | ✓ | ✓ | ✓ | none |
| `GET /api/users/{u}/details` (someone else's) | ✓ | – | – (status only, in `GET /api/users`) | read |
| `POST /api/users/{u}/details/verify`, `/return` | ✓ (never your own) | – | – | none (the decision is journaled) |
| `GET /api/access-requests` | ✓ | – | ✓ | read |
| `POST /api/access-requests/{id}/approve`, `/decline` | ✓ | – | – | none (creates the auditor on approval; the decision is journaled) |
| `GET /api/accounts`, `/api/accounts/{no}`, `/statement` | all | own only | all | read |
| `GET /api/payees/{no}` | ✓ | ✓ | ✓ | read (name and status only) |
| `GET /api/reviews`, `POST /api/reviews/{seq}/ai-note` | ✓ | – | ✓ | none (an AI note is journaled) |
| `GET /api/audit/proof`, `/journal`, `/flags`, `/duplicates`, `/export` | ✓ | – | ✓ | read |
| `POST /api/audit/reconciliation` | ✓ | – | ✓ | none (the summary is journaled) |
| `GET /api/entries/{seq}` | ✓ | entries touching own accounts | ✓ | read + hash/signature check |
| `GET /api/public-key`, `GET /api/health` | public | public | public | read |
| `POST /api/auth/register` (sign-up) | public | public | public | none: an account holder with one ₹0 account, or a request for auditor access |

---

## Authentication

### `POST /api/auth/login`
Anyone. Body `{"username": "priya", "password": "…"}`.
`200` → `{"access_token", "token_type": "bearer", "expires_in": 3600, "user": {"username", "name", "role", "main_admin", "permissions": [...]}}`. `main_admin` is `true` only for the main administrator.
`401 BAD_CREDENTIALS`. `429 TOO_MANY_REQUESTS` after 10 failures in 5 minutes from one IP.
`403 APPROVAL_PENDING` or `403 ACCESS_DECLINED` when the username belongs to an auditor request (not a user) and the password matches it: no token. A wrong password gets the usual `401`, so nobody learns a request exists without its password.

### `POST /api/auth/register`
Anyone (public sign-up, the "Create user" tab on the home page). Body `{"username": "kavya", "display_name": "Kavya Reddy", "password": "at-least-10-chars", "role": "holder"}`. `role` is `"holder"` (the default) or `"auditor"`; anything else, `"admin"` included, returns `422`, as does any unknown field such as `balance`.

* `"holder"` → `201`: creates an **account holder** with one account named "Main account" at **₹0.00**, and signs them in. Same shape as login, plus `"account": {"account_no", "name", "balance"}`. The new user and account are journaled (`USER_CREATED`, `ACCOUNT_OPENED`, marked `"via": "self-service sign-up"`).
* `"auditor"` → `202`: files a **request** and creates no user. No token: `{"status": "pending", "message", "request": {"id", "username", "name", "role": "auditor", "status": "pending", "requested_at", ...}}`. Signing in answers `403 APPROVAL_PENDING` until an administrator decides.

`409` username taken (by a user or by a request still waiting) · `422` invalid input · `429 TOO_MANY_REQUESTS` after 10 sign-up attempts in 10 minutes from one address. Administrators are created only by the main administrator (`POST /api/users`).

### `POST /api/auth/logout`
Any signed-in user. Revokes **all** of that user's tokens. `200 {"ok": true}`.

### `GET /api/me`
Any signed-in user. Returns username, name, role, `main_admin` and permissions (read fresh from the database; the web UI asks for it on every page load).

---

## Money

All money endpoints need `Idempotency-Key`. Typical success body:

```json
{
  "status": "posted",
  "entry": {"seq": 42, "kind": "TRANSFER", "created_at": "2026-11-06T17:05:11.120443Z",
            "account": "LL-1001", "counterparty": "LL-1003",
            "amount": {"paise": 75050, "rupees": "750.50", "display": "Rs 750.50"},
            "memo": "Lunch", "ref_seq": null},
  "receipt": {"seq": 42, "entry_id": "…", "created_at": "…", "hash": "9f…", "prev_hash": "3a…",
              "key_id": "e1cdfa83c60e8596", "signature": "base64…"},
  "balances": {"LL-1001": {"paise": 6179950, "rupees": "61799.50", "display": "Rs 61,799.50"}}
}
```

`balances` only lists accounts the caller may see. `receipt.signature` is an Ed25519 signature over `receipt.hash`. Check it with `GET /api/public-key`.

### `POST /api/deposits` (admin)
Records money that **arrived from outside** (cash, a bank transfer). Body `{"account": "LL-1001", "amount": "5000.00", "memo": "NEFT ref 0042"}`.
Effect: `SYS-EXTERNAL −amount`, account `+amount`.
`201` posted · `403 ROLE_NOT_PERMITTED` (not admin; journaled) · `409 ACCOUNT_NOT_ACTIVE` · `404` unknown account.

### `POST /api/withdrawals` (holder, own account)
Body `{"account": "LL-1001", "amount": "600", "memo": "ATM"}`.
Effect: account `−amount`, `SYS-EXTERNAL +amount`. If Sentinel holds it, account `−amount` → `SYS-SUSPENSE` and the response is `202` with `status: "held"`.
`201` · `202` held · `403 NOT_ACCOUNT_OWNER` / `ROLE_NOT_PERMITTED` · `409 INSUFFICIENT_FUNDS` / `DAILY_LIMIT_EXCEEDED` / `ACCOUNT_NOT_ACTIVE`.

### `POST /api/transfers` (holder, from own account)
Body `{"from_account": "LL-1001", "to_account": "LL-1003", "amount": "750.50", "memo": "Lunch"}`.
Effect: source `−amount`, destination `+amount`, in one transaction. If held: source `−amount` → `SYS-SUSPENSE` and `202`. Holders are told only that it is waiting for review, not which rules fired.
`201` · `202` held · `403 NOT_ACCOUNT_OWNER` / `ROLE_NOT_PERMITTED` · `404 DESTINATION_NOT_FOUND` · `409 INSUFFICIENT_FUNDS` / `DAILY_LIMIT_EXCEEDED` / `ACCOUNT_NOT_ACTIVE` / `DESTINATION_NOT_ACTIVE` · `422 SAME_ACCOUNT` / `DESTINATION_NOT_ALLOWED`.

### `POST /api/reviews/{hold_seq}/approve` and `/reject` (admin)
Body `{"note": "Confirmed with the CFO by phone"}` (recorded).
Approve: `SYS-SUSPENSE −amount`, intended destination `+amount`. Reject: `SYS-SUSPENSE −amount`, sender `+amount`.
The decision entry records the reviewer, the note, Sentinel's suggestion (score and reasons) and any AI notes seen.
`201` · `409 HOLD_ALREADY_DECIDED` (each hold is decided exactly once, also under concurrency) · `409 DESTINATION_NOT_ACTIVE` (approve only).

### `POST /api/reversals` (admin)
Body `{"entry_seq": 42, "reason": "Customer complaint #77: wrong payee"}`.
A **new** entry moves the money back the way it came: transfer → payee to payer; deposit → account to external; withdrawal → external to account; approved hold → payee to payer. The original entry is never edited.
`201` · `409 ALREADY_REVERSED` · `409 INSUFFICIENT_FUNDS` (the payee already spent it; no overdraw) · `422 NOT_REVERSIBLE` (refusals, holds, reversals).

---

## Accounts

* `GET /api/accounts`: holders get their own accounts; admins and auditors get all, including `SYS-EXTERNAL` and `SYS-SUSPENSE`. Each account shows `balance`, `available` (balance + overdraft), `pending_review`, `overdraft_limit`, `daily_limit` and `status`.
* `POST /api/accounts` (admin): `{"owner_username": "rahul", "name": "Travel"}` → `201`, account at ₹0. Any extra field (e.g. `"balance"`) → `422`.
* `GET /api/accounts/{no}`: holder (own) or admin/auditor. `403` for other holders' accounts.
* `GET /api/accounts/{no}/statement?limit=100&before_seq=N`: every posting **and every refused attempt** on the account, newest first. Each row has `balance_after` (the running balance computed by the database; for refusals, the balance at that moment), `initiated_by`, `direction`, `status`, `reason_code`, `hold` state and `hash`. Page with `next_before_seq`.
* `GET /api/payees/{no}`: any signed-in user. Returns the owner's name, the account name and status, never a balance.
* `POST /api/accounts/{no}/freeze` | `/unfreeze` | `/close` (admin): `{"reason": "…"}`. Closing needs a zero balance and no payments under review.
* `POST /api/accounts/{no}/overdraft` (admin): `{"limit": "5000", "reason": "…"}`, ₹0–₹50,000. `409` if the balance is already below the new floor.

## Users and administrators

The first administrator is the **main administrator**. Any administrator manages holders and auditors; only the main administrator manages administrators; nobody can remove the main administrator. Nothing is ever deleted: removing access ends every session at once and keeps the history.

* `GET /api/users` (admin, auditor): everyone, with `username`, `name`, `role`, `main_admin`, `active`, number of accounts, and the status of their personal details (`details`: `none`, `waiting`, `verified` or `returned`, plus `details_version`). Never the details themselves.
* `POST /api/users` (admin): `{"username", "display_name", "role": "holder" | "auditor" | "admin", "password"}` → `201`, journaled `USER_CREATED` naming who created it. `"role": "admin"` needs the main administrator: anyone else gets `403 MAIN_ADMIN_ONLY`. `409` username taken.
* `POST /api/users/{u}/deactivate` and `/activate` (admin): removes or restores access, journaled `USER_STATUS`. `200` → `{"username", "active"}`. For an administrator's access: `403 MAIN_ADMIN_ONLY` unless you are the main administrator. For the main administrator: `403 MAIN_ADMIN_PROTECTED`, whoever asks. Your own access: `409`.
* The database enforces the same: one main administrator, set once (`LL013`); never deactivated (`LL015`); administrators created only by the main administrator, on the record (`LL014`); an administrator's access changed only with the main administrator's signed `USER_STATUS` entry (`LL016`).

## Personal details

The basic details a bank keeps: date of birth, mobile, email, address, occupation and an optional nominee. No ID numbers. Only the person changes them; any administrator except that person verifies a version or sends it back. The person and administrators see them; auditors see only the status. The details never enter the journal: `DETAILS_SUBMITTED`, `DETAILS_VERIFIED` and `DETAILS_RETURNED` entries record who did what to which version (and which fields changed), never a value.

* `GET /api/me/details` (anyone signed in): `{"username", "name", "status", "version", "details": {...} | null, "submitted_at", "decided_by", "decided_at", "return_note", "choices": {"states", "occupations", "relations"}}`. `status` is `none` (nothing given yet), `waiting`, `verified` or `returned` (sent back, with `return_note`).
* `PUT /api/me/details` (anyone signed in), body:
  ```json
  {"date_of_birth": "1996-04-12", "mobile": "98765 43210", "email": "priya@example.com",
   "address_line1": "Flat 302, Lakeview Residency", "address_line2": "Madhapur", "city": "Hyderabad",
   "state": "Telangana", "pin_code": "500081", "occupation": "salaried",
   "nominee_name": "Rakesh Sharma", "nominee_relation": "father"}
  ```
  → `200`, same shape as GET. Any change is a new `version` with status `waiting`; saving the same details again changes and records nothing. `422` for: a mobile number that is not 10 digits starting 6–9 (`+91`, spaces and hyphens are accepted), a PIN code that is not 6 digits or starts with 0, a malformed email, a date of birth that is not a real `YYYY-MM-DD` date or makes you younger than 18, a state that is not an Indian state or union territory, an unknown occupation or relation, a nominee name without a relation (or the reverse), control or direction-changing characters, and any unknown field (`pan`, `aadhaar`, `status`...).
* `GET /api/users/{u}/details` (admin): the same view of that person's details. `403` for holders and auditors.
* `POST /api/users/{u}/details/verify` (admin), body `{"version": 2}`: verifies exactly that version. `POST /api/users/{u}/details/return` (admin), body `{"version": 2, "note": "PIN code does not match the address proof"}`: sends it back; the note (required, ≤200 characters) is shown to the person and stored with the details, not in the journal. Both `200` → the new view. `403 OWN_DETAILS` for your own details · `404` nothing given yet · `409 DETAILS_CHANGED` the person saved a newer version meanwhile (open it again) · `409 ALREADY_DECIDED` that version was already verified or sent back.
* The database enforces the same (`LL017`): a new version needs a `DETAILS_SUBMITTED` entry by the person; a decision needs a `DETAILS_VERIFIED` / `DETAILS_RETURNED` entry by an administrator who is not the person, for that version; a change never keeps an old decision; details are never deleted (`LL010`).

## Auditor requests (from sign-up)

* `GET /api/access-requests?state=pending|decided` (admin, auditor): waiting requests oldest first, or the last 100 decisions with who decided, when, the note and the `decision_seq` journal entry. Never a password hash.
* `POST /api/access-requests/{id}/approve` (admin), body `{"note": "…"}` (optional, ≤140 characters, recorded): creates the **auditor** from the stored request (never from this body) and journals `ACCESS_DECISION` followed by `USER_CREATED`, which points back at the decision (`ref_seq`). `200` → `{"request", "user": {"username", "name", "role": "auditor"}, "entry": {"seq"}}`.
* `POST /api/access-requests/{id}/decline` (admin), same body: journals `ACCESS_DECISION` and creates no user. `200` → `{"request", "user": null, "entry"}`. The person can still ask again later; that is a new request and a new decision.
* A request is decided **once**. The second decision, including two administrators clicking at the same moment, gets `409 ALREADY_DECIDED`. `404` for an unknown id, `403` for anyone but an administrator. The database refuses a decision that is not journaled by an administrator, or a second one (`LL012`).

## Review and audit (admin, auditor)

* `GET /api/reviews?state=pending|decided|all`: held payments with Sentinel's score, reasons and features, the latest AI note, and the decision if any.
* `POST /api/reviews/{seq}/ai-note`: asks the LLM (if configured) for a reviewer note. No money moves; the note is journaled as `AI_NOTE` with the model name. The same facts reuse the cached note.
* `GET /api/audit/proof`: replays the whole journal and returns 15 checks (chain, signatures, head, double entry, replay, floors, stored balances, conservation, suspense, single decisions, exactly-once, refusal balances, access, personal details, backstop) plus totals. *Access* checks that every user has exactly one `USER_CREATED` entry with the role it has now, that every auditor was created by another administrator, that the main-administrator flag matches the signed record, and that every administrator was created by the rule in force at the time (by another administrator before the upgrade's `MAIN_ADMIN` entry, by the main administrator after it). *Personal details* replays every `DETAILS_*` entry (each version from its owner, one after another; each decision from another administrator, once, on the current version) and compares the result with the stored details.
* `POST /api/audit/reconciliation`: the proof plus a plain-English summary (LLM if configured, template otherwise), journaled.
* `GET /api/audit/journal?kind=&status=&account=&before_seq=&limit=`: the hash-chained journal.
* `GET /api/audit/flags`: held payments, elevated scores, authorisation refusals, database backstops (should be empty), reversals, control changes (including auditor-access decisions), large deposits, key reuse.
* `GET /api/audit/duplicates`: every replayed request, key reuse and in-flight retry, with the original entry.
* `GET /api/audit/export`: evidence bundle for `python tools/verify_bundle.py bundle.json --public-key <key>`.

## Entries and public endpoints

* `GET /api/entries/{seq}`: one journal entry with `verification.hash_matches_content` and `verification.signature_valid`. Holders may fetch entries that touch their own accounts (receipt check).
* `GET /api/public-key`: the Ed25519 public key and key id. Public by design.
* `GET /api/health`: liveness plus the current chain head (`seq`, `hash`). Public on purpose: anyone can note it down and later prove history was not rewritten.

## Status and reason codes

| HTTP | code | meaning | journaled |
|---|---|---|:-:|
| 401 | `UNAUTHORIZED`, `TOKEN_INVALID`, `TOKEN_EXPIRED`, `SESSION_REVOKED`, `BAD_CREDENTIALS` | sign in again | – |
| 403 | `ROLE_NOT_PERMITTED`, `NOT_ACCOUNT_OWNER` | not allowed | ✓ |
| 403 | `APPROVAL_PENDING`, `ACCESS_DECLINED` | sign-in for an auditor request that is waiting / was declined (right password only) | – |
| 403 | `FORBIDDEN` | an admin-only or staff-only endpoint | – |
| 403 | `OWN_DETAILS` | nobody verifies or sends back their own personal details | – |
| 409 | `DETAILS_CHANGED` | the personal details changed since you opened them; check the new version | – |
| 403 | `MAIN_ADMIN_ONLY`, `MAIN_ADMIN_PROTECTED` | creating an administrator or changing an administrator's access needs the main administrator / nobody can remove the main administrator | the change itself is, when it happens |
| 404 | `NOT_FOUND`, `DESTINATION_NOT_FOUND` | no such thing | dest ✓ |
| 409 | `INSUFFICIENT_FUNDS`, `DAILY_LIMIT_EXCEEDED`, `ACCOUNT_NOT_ACTIVE`, `DESTINATION_NOT_ACTIVE`, `HOLD_ALREADY_DECIDED`, `ALREADY_REVERSED` | refused by a business rule | ✓ |
| 409 | `INTEGRITY_BACKSTOP` | the database refused a write the app attempted (a bug signal); nothing moved | ✓ |
| 409 | `IDEMPOTENCY_KEY_REUSED`, `REQUEST_IN_PROGRESS` | key misuse / first request still running; nothing moved | in the duplicates log |
| 409 | `ALREADY_DECIDED`, `CONFLICT` | auditor request or version of personal details already decided / username taken, or another state conflict | – |
| 422 | `VALIDATION_FAILED`, `SAME_ACCOUNT`, `DESTINATION_NOT_ALLOWED`, `NOT_REVERSIBLE` | malformed or impossible | the last three ✓ |
| 428 | `IDEMPOTENCY_KEY_REQUIRED` | send an Idempotency-Key | – |
| 429 | `TOO_MANY_REQUESTS` | login or sign-up throttled | – |
| 503 | `BUSY` | lock or statement timeout; nothing moved, retry with the same key | – |

## curl examples

```bash
URL=http://localhost:8000
TOKEN=$(curl -s $URL/api/auth/login -H 'content-type: application/json' \
  -d '{"username":"priya","password":"LedgerPriya#2026"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# pay Rahul; run it twice: the second call says Idempotent-Replayed: true and moves nothing
curl -i $URL/api/transfers -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -H 'Idempotency-Key: demo-0001-rent' -d '{"from_account":"LL-1001","to_account":"LL-1003","amount":"750.50"}'

# two withdrawals at the same instant from the same account
for i in 1 2; do curl -s $URL/api/withdrawals -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -H "Idempotency-Key: race-$i-$RANDOM" -d '{"account":"LL-1001","amount":"40000"}' & done; wait

# ask for auditor access (202, no token), then try to sign in (403 APPROVAL_PENDING)
curl -s $URL/api/auth/register -H 'content-type: application/json' \
  -d '{"username":"kavya","display_name":"Kavya Reddy","password":"kavya-password-1","role":"auditor"}'
curl -s $URL/api/auth/login -H 'content-type: application/json' -d '{"username":"kavya","password":"kavya-password-1"}'

# as admin: see the queue, approve request 1 with a note (the seeded request from "meera" is waiting too)
ADMIN=$(curl -s $URL/api/auth/login -H 'content-type: application/json' \
  -d '{"username":"admin","password":"LedgerAdmin#2026"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s $URL/api/access-requests -H "Authorization: Bearer $ADMIN"
curl -s $URL/api/access-requests/1/approve -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' \
  -d '{"note":"confirmed with the audit firm"}'

# as the main administrator: create a second administrator
curl -s $URL/api/users -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' \
  -d '{"username":"ravi","display_name":"Ravi Kumar","role":"admin","password":"ravi-password-1"}'
RAVI=$(curl -s $URL/api/auth/login -H 'content-type: application/json' \
  -d '{"username":"ravi","password":"ravi-password-1"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
# ravi does the daily work, but cannot make administrators (403 MAIN_ADMIN_ONLY) or remove the main one (403 MAIN_ADMIN_PROTECTED)
curl -s $URL/api/users -H "Authorization: Bearer $RAVI" -H 'content-type: application/json' \
  -d '{"username":"ravi2","display_name":"Another Admin","role":"admin","password":"ravi-password-2"}'
curl -s -X POST $URL/api/users/admin/deactivate -H "Authorization: Bearer $RAVI"
```
