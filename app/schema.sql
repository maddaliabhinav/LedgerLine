-- =============================================================================
-- LedgerLine schema (version 1)
--
-- Design rule: every invariant that protects money is enforced HERE, by
-- PostgreSQL, and not only in Python. If the application has a bug, the
-- database refuses the write and the transaction rolls back.
--
--   accounts.balance_floor ........ CHECK: no customer balance below -overdraft_limit
--   accounts_guard ................ balance changes ONLY through a posting (trigger)
--   postings_apply ................ applies a posting to the balance and refuses a
--                                   posting computed from a stale balance (LL004)
--   journal_postings_match ........ deferred: a posted entry's postings sum to zero
--                                   and total its amount; refusals carry no postings
--   journal_before_insert ......... every entry extends the hash chain head (gapless)
--   forbid_mutation ............... journal / postings / replays are append-only
--   idempotency_write_once ........ an idempotency record is completed once, never edited
--   unique indexes ................ one entry per idempotency key, one decision per
--                                   hold, one reversal per entry
--
-- Error codes raised by our triggers use the custom SQLSTATE class "LL".
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS ledgerline;
SET search_path TO ledgerline;

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Generic guard: used to make tables append-only.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on % is not allowed: LedgerLine records are permanent (append-only)', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'LL010';
END
$$;

-- -----------------------------------------------------------------------------
-- Users
-- -----------------------------------------------------------------------------
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'),
    display_name  TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    role          TEXT NOT NULL CHECK (role IN ('admin', 'holder', 'auditor')),
    password_hash TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    token_version INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Users are deactivated, never deleted: journal entries point at them forever.
CREATE TRIGGER users_no_delete BEFORE DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER users_no_truncate BEFORE TRUNCATE ON users
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- -----------------------------------------------------------------------------
-- Accounts. Money is stored as BIGINT paise (1 rupee = 100 paise). No floats.
--
-- System accounts:
--   SYS-EXTERNAL : the outside world (bank rails / cash). Deposits come from it,
--                  withdrawals go to it. Its balance is minus the net money that
--                  has entered LedgerLine, so it is the only account allowed to
--                  be negative.
--   SYS-SUSPENSE : transfers held for human review are parked here. Floor 0.
-- -----------------------------------------------------------------------------
CREATE SEQUENCE account_no_seq START 1001;

CREATE TABLE accounts (
    id              BIGSERIAL PRIMARY KEY,
    account_no      TEXT NOT NULL UNIQUE CHECK (account_no ~ '^(LL-[0-9]{4,8}|SYS-[A-Z]{3,12})$'),
    owner_id        BIGINT REFERENCES users(id),
    name            TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    kind            TEXT NOT NULL CHECK (kind IN ('customer', 'system')),
    currency        TEXT NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
    balance         BIGINT NOT NULL DEFAULT 0,
    overdraft_limit BIGINT NOT NULL DEFAULT 0 CHECK (overdraft_limit BETWEEN 0 AND 5000000),
    daily_limit     BIGINT NOT NULL DEFAULT 50000000 CHECK (daily_limit > 0),
    allow_negative  BOOLEAN NOT NULL DEFAULT FALSE,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'frozen', 'closed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- THE floor. Checked by PostgreSQL on every balance change, atomically.
    CONSTRAINT balance_floor CHECK (allow_negative OR balance >= -overdraft_limit),
    CONSTRAINT owner_matches_kind CHECK ((kind = 'customer') = (owner_id IS NOT NULL)),
    CONSTRAINT negative_only_for_system CHECK (NOT allow_negative OR kind = 'system'),
    CONSTRAINT system_accounts_no_overdraft CHECK (kind = 'customer' OR overdraft_limit = 0)
);
CREATE INDEX accounts_owner ON accounts (owner_id);

CREATE OR REPLACE FUNCTION accounts_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.balance <> 0 THEN
            RAISE EXCEPTION 'accounts open with a zero balance; money only enters through a posting'
                USING ERRCODE = 'LL001';
        END IF;
        RETURN NEW;
    END IF;

    -- A balance may only change from inside postings_apply (trigger depth 2).
    -- A direct "UPDATE accounts SET balance = ..." from anywhere is refused.
    IF NEW.balance IS DISTINCT FROM OLD.balance AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'balance of % can only change through a ledger posting', OLD.account_no
            USING ERRCODE = 'LL002';
    END IF;

    IF NEW.id <> OLD.id
       OR NEW.account_no <> OLD.account_no
       OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.kind <> OLD.kind
       OR NEW.currency <> OLD.currency
       OR NEW.allow_negative <> OLD.allow_negative
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'identity columns of account % are immutable', OLD.account_no
            USING ERRCODE = 'LL003';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER accounts_guard BEFORE INSERT OR UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION accounts_guard();
CREATE TRIGGER accounts_no_delete BEFORE DELETE ON accounts
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER accounts_no_truncate BEFORE TRUNCATE ON accounts
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- -----------------------------------------------------------------------------
-- The chain head: one row holding the last sequence number and hash.
-- Locking this row (SELECT ... FOR UPDATE) is what serialises appends, which
-- makes the journal a single, gapless, totally ordered hash chain.
-- -----------------------------------------------------------------------------
CREATE TABLE ledger_head (
    id         SMALLINT PRIMARY KEY CHECK (id = 1),
    last_seq   BIGINT NOT NULL,
    last_hash  TEXT NOT NULL,
    last_at    TIMESTAMPTZ NOT NULL
);
INSERT INTO ledger_head (id, last_seq, last_hash, last_at)
VALUES (1, 0, repeat('0', 64), '2000-01-01T00:00:00Z');

-- -----------------------------------------------------------------------------
-- The journal: one row per thing that happened. Money movements (posted),
-- refused attempts (refused) and administrative / AI events (info).
-- Hash-chained (prev_hash -> hash) and Ed25519-signed by the application.
-- -----------------------------------------------------------------------------
CREATE TABLE journal (
    seq              BIGINT PRIMARY KEY CHECK (seq > 0),
    entry_id         UUID NOT NULL UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN (
                        'GENESIS', 'DEPOSIT', 'WITHDRAWAL', 'TRANSFER', 'HOLD',
                        'HOLD_APPROVED', 'HOLD_REJECTED', 'REVERSAL',
                        'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
                        'USER_CREATED', 'USER_STATUS', 'AI_NOTE')),
    status           TEXT NOT NULL CHECK (status IN ('posted', 'refused', 'info')),
    actor_id         BIGINT REFERENCES users(id),
    account_id       BIGINT REFERENCES accounts(id),
    counterparty_id  BIGINT REFERENCES accounts(id),
    amount           BIGINT CHECK (amount IS NULL OR amount > 0),
    currency         TEXT CHECK (currency IS NULL OR currency = 'INR'),
    idempotency_key  TEXT,
    reason_code      TEXT,
    memo             TEXT CHECK (memo IS NULL OR length(memo) <= 140),
    ref_seq          BIGINT REFERENCES journal(seq),
    observed_balance BIGINT,
    details          JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash        TEXT NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    hash             TEXT NOT NULL UNIQUE CHECK (hash ~ '^[0-9a-f]{64}$'),
    key_id           TEXT NOT NULL,
    signature        TEXT NOT NULL,
    txid             BIGINT NOT NULL,

    CONSTRAINT money_kinds_carry_amount CHECK (
        (kind IN ('DEPOSIT', 'WITHDRAWAL', 'TRANSFER', 'HOLD', 'HOLD_APPROVED',
                  'HOLD_REJECTED', 'REVERSAL')) = (amount IS NOT NULL)),
    CONSTRAINT event_kinds_are_info CHECK (
        (kind IN ('GENESIS', 'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
                  'USER_CREATED', 'USER_STATUS', 'AI_NOTE')) = (status = 'info')),
    CONSTRAINT refusals_carry_reason CHECK ((status = 'refused') = (reason_code IS NOT NULL))
);

-- Exactly-once backstop: one journal entry per (user, idempotency key).
CREATE UNIQUE INDEX journal_idempotency
    ON journal (actor_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
-- A held transfer can be decided (approved or rejected) exactly once.
CREATE UNIQUE INDEX journal_one_decision_per_hold
    ON journal (ref_seq) WHERE kind IN ('HOLD_APPROVED', 'HOLD_REJECTED') AND status = 'posted';
-- A posted entry can be reversed exactly once.
CREATE UNIQUE INDEX journal_one_reversal_per_entry
    ON journal (ref_seq) WHERE kind = 'REVERSAL' AND status = 'posted';

CREATE INDEX journal_account      ON journal (account_id, seq);
CREATE INDEX journal_counterparty ON journal (counterparty_id, seq);
CREATE INDEX journal_kind         ON journal (kind, seq);
CREATE INDEX journal_actor_time   ON journal (actor_id, created_at);
CREATE INDEX journal_ref          ON journal (ref_seq);

CREATE OR REPLACE FUNCTION journal_before_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    h ledger_head%ROWTYPE;
BEGIN
    SELECT * INTO h FROM ledger_head WHERE id = 1;
    IF NEW.seq <> h.last_seq + 1 OR NEW.prev_hash <> h.last_hash THEN
        RAISE EXCEPTION 'journal entry % does not extend the chain head (head is %)', NEW.seq, h.last_seq
            USING ERRCODE = 'LL005';
    END IF;
    IF NEW.created_at < h.last_at THEN
        RAISE EXCEPTION 'journal entry % is back-dated (% < %)', NEW.seq, NEW.created_at, h.last_at
            USING ERRCODE = 'LL005';
    END IF;
    -- Remember which transaction wrote the entry; postings must come from the same one.
    NEW.txid := txid_current();
    RETURN NEW;
END
$$;
CREATE TRIGGER journal_before_insert BEFORE INSERT ON journal
    FOR EACH ROW EXECUTE FUNCTION journal_before_insert();
CREATE TRIGGER journal_append_only BEFORE UPDATE OR DELETE ON journal
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER journal_no_truncate BEFORE TRUNCATE ON journal
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE OR REPLACE FUNCTION ledger_head_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.last_seq <> OLD.last_seq + 1 OR NOT EXISTS (
        SELECT 1 FROM journal
         WHERE seq = NEW.last_seq AND hash = NEW.last_hash
           AND prev_hash = OLD.last_hash AND created_at = NEW.last_at) THEN
        RAISE EXCEPTION 'chain head can only advance by one entry that links to it'
            USING ERRCODE = 'LL006';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER ledger_head_guard BEFORE UPDATE ON ledger_head
    FOR EACH ROW EXECUTE FUNCTION ledger_head_guard();
CREATE TRIGGER ledger_head_no_delete BEFORE DELETE ON ledger_head
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_head_no_truncate BEFORE TRUNCATE ON ledger_head
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- -----------------------------------------------------------------------------
-- Postings: the double-entry lines. amount is signed: positive raises the
-- account balance (credit), negative lowers it (debit). Every posted journal
-- entry has >= 2 postings that sum to exactly zero.
-- -----------------------------------------------------------------------------
CREATE TABLE postings (
    id            BIGSERIAL PRIMARY KEY,
    entry_seq     BIGINT NOT NULL REFERENCES journal(seq),
    line_no       SMALLINT NOT NULL CHECK (line_no > 0),
    account_id    BIGINT NOT NULL REFERENCES accounts(id),
    amount        BIGINT NOT NULL CHECK (amount <> 0),
    balance_after BIGINT NOT NULL,
    UNIQUE (entry_seq, line_no),
    UNIQUE (entry_seq, account_id)
);
CREATE INDEX postings_account ON postings (account_id, entry_seq);

CREATE OR REPLACE FUNCTION postings_apply() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    j_txid   BIGINT;
    j_status TEXT;
    actual   BIGINT;
BEGIN
    SELECT txid, status INTO j_txid, j_status FROM journal WHERE seq = NEW.entry_seq;
    IF j_txid IS NULL OR j_txid <> txid_current() THEN
        RAISE EXCEPTION 'postings must be written in the same transaction as journal entry %', NEW.entry_seq
            USING ERRCODE = 'LL008';
    END IF;
    IF j_status <> 'posted' THEN
        RAISE EXCEPTION 'journal entry % is %, only posted entries carry postings', NEW.entry_seq, j_status
            USING ERRCODE = 'LL008';
    END IF;

    -- Apply the posting. The balance_floor CHECK on accounts is evaluated by
    -- this UPDATE: an overdraw raises check_violation (23514) right here.
    UPDATE accounts SET balance = balance + NEW.amount
     WHERE id = NEW.account_id
    RETURNING balance INTO actual;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'posting to unknown account %', NEW.account_id USING ERRCODE = 'LL008';
    END IF;

    -- Concurrency tripwire: the application states the balance it expects.
    -- If that differs from reality, the app decided on a stale read.
    IF actual <> NEW.balance_after THEN
        RAISE EXCEPTION 'stale balance on account %: posting expected %, database has %',
            NEW.account_id, NEW.balance_after, actual
            USING ERRCODE = 'LL004';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER postings_apply BEFORE INSERT ON postings
    FOR EACH ROW EXECUTE FUNCTION postings_apply();
CREATE TRIGGER postings_append_only BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER postings_no_truncate BEFORE TRUNCATE ON postings
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Conservation, checked at COMMIT for every journal entry written in the
-- transaction: posted => >= 2 postings, credits == amount, sum == 0.
-- Anything else => no postings at all.
CREATE OR REPLACE FUNCTION journal_postings_match() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    n       INTEGER;
    credits BIGINT;
    total   BIGINT;
BEGIN
    SELECT count(*), COALESCE(sum(amount) FILTER (WHERE amount > 0), 0), COALESCE(sum(amount), 0)
      INTO n, credits, total
      FROM postings WHERE entry_seq = NEW.seq;
    IF NEW.status = 'posted' THEN
        IF n < 2 OR credits <> NEW.amount OR total <> 0 THEN
            RAISE EXCEPTION 'posted entry % is unbalanced (lines=%, credits=%, sum=%, amount=%): money would be created or destroyed',
                NEW.seq, n, credits, total, NEW.amount
                USING ERRCODE = 'LL007';
        END IF;
    ELSIF n > 0 THEN
        RAISE EXCEPTION '% entry % cannot carry postings', NEW.status, NEW.seq USING ERRCODE = 'LL007';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER journal_postings_match AFTER INSERT ON journal
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION journal_postings_match();

-- -----------------------------------------------------------------------------
-- Idempotency. The key row is INSERTed at the very start of the money
-- transaction and completed before COMMIT, in the same transaction as the
-- money movement. A concurrent duplicate blocks on the primary key until the
-- first commits, then reads the stored response.
-- Keys are kept forever: expiring them would reopen double payment.
-- -----------------------------------------------------------------------------
CREATE TABLE idempotency_keys (
    user_id      BIGINT NOT NULL REFERENCES users(id),
    idem_key     TEXT NOT NULL CHECK (idem_key ~ '^[A-Za-z0-9_.:-]{8,100}$'),
    endpoint     TEXT NOT NULL,
    fingerprint  TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    http_status  INTEGER,
    -- The exact bytes sent to the client. TEXT, not JSONB: JSONB would re-order
    -- keys and a replay would no longer be byte-for-byte the first answer.
    response     TEXT,
    entry_seq    BIGINT REFERENCES journal(seq),
    PRIMARY KEY (user_id, idem_key)
);

CREATE OR REPLACE FUNCTION idempotency_write_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.completed_at IS NOT NULL THEN
            RAISE EXCEPTION 'idempotency record % is final', OLD.idem_key USING ERRCODE = 'LL011';
        END IF;
        IF NEW.user_id <> OLD.user_id OR NEW.idem_key <> OLD.idem_key
           OR NEW.endpoint <> OLD.endpoint OR NEW.fingerprint <> OLD.fingerprint
           OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'idempotency identity is immutable' USING ERRCODE = 'LL011';
        END IF;
        IF NEW.completed_at IS NULL OR NEW.response IS NULL OR NEW.http_status IS NULL THEN
            RAISE EXCEPTION 'an idempotency record can only be updated to complete it' USING ERRCODE = 'LL011';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% on idempotency_keys is not allowed', TG_OP USING ERRCODE = 'LL010';
END
$$;
CREATE TRIGGER idempotency_write_once BEFORE UPDATE OR DELETE ON idempotency_keys
    FOR EACH ROW EXECUTE FUNCTION idempotency_write_once();
CREATE TRIGGER idempotency_no_truncate BEFORE TRUNCATE ON idempotency_keys
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- A key may never be committed half-done (a retry would find no answer).
CREATE OR REPLACE FUNCTION idempotency_completed_at_commit() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM idempotency_keys
                    WHERE user_id = NEW.user_id AND idem_key = NEW.idem_key
                      AND completed_at IS NOT NULL) THEN
        RAISE EXCEPTION 'idempotency record % committed without a result', NEW.idem_key
            USING ERRCODE = 'LL011';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER idempotency_completed_at_commit AFTER INSERT ON idempotency_keys
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION idempotency_completed_at_commit();

-- Every duplicate we answered from storage, and every misuse of a key.
-- This is how we show "which requests were duplicates" in the data.
CREATE TABLE idempotency_replays (
    id                 BIGSERIAL PRIMARY KEY,
    user_id            BIGINT NOT NULL REFERENCES users(id),
    idem_key           TEXT NOT NULL,
    endpoint           TEXT NOT NULL,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    outcome            TEXT NOT NULL CHECK (outcome IN (
                          'replayed', 'key_reused_with_different_request', 'in_flight_timeout')),
    fingerprint        TEXT NOT NULL,
    original_entry_seq BIGINT REFERENCES journal(seq)
);
CREATE INDEX idempotency_replays_user ON idempotency_replays (user_id, received_at);
CREATE TRIGGER idempotency_replays_append_only BEFORE UPDATE OR DELETE ON idempotency_replays
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER idempotency_replays_no_truncate BEFORE TRUNCATE ON idempotency_replays
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- -----------------------------------------------------------------------------
-- System accounts (created by the migration, never by the API).
-- -----------------------------------------------------------------------------
INSERT INTO accounts (account_no, owner_id, name, kind, allow_negative)
VALUES ('SYS-EXTERNAL', NULL, 'External world (bank rails, cash)', 'system', TRUE),
       ('SYS-SUSPENSE', NULL, 'Held for review (suspense)',       'system', FALSE);

INSERT INTO schema_version (version) VALUES (1);
