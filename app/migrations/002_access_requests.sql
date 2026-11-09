-- =============================================================================
-- LedgerLine schema version 2: who may become an auditor.
--
-- Anyone can ASK for auditor access when they sign up; only an administrator
-- can grant it. Until then there is no user row, so there is nothing to sign
-- in with. As with money, the rules are enforced HERE as well as in Python:
--
--   access_requests.role ............ CHECK: only auditor access can be requested
--                                     (administrator access is never self-service)
--   access_requests_one_pending ..... one open request per username
--   access_requests_guard ........... a request starts pending and is decided once,
--                                     by an administrator, with a signed journal
--                                     entry; after that it never changes (LL012)
--   users_guard ..................... a user keeps the username and role it was
--                                     created with (LL013)
--   users_created_on_record ......... at COMMIT, every new user has a USER_CREATED
--                                     entry with the same role; an auditor or admin
--                                     only from another administrator, or from the
--                                     server's own command line (LL014)
--
-- Applied on top of schema.sql for a new database, and by `python -m app.cli
-- migrate` to upgrade an existing one.
-- =============================================================================

SET search_path TO ledgerline;

-- Decisions on access requests are journal entries, like everything else.
ALTER TABLE journal DROP CONSTRAINT journal_kind_check;
ALTER TABLE journal ADD CONSTRAINT journal_kind_check CHECK (kind IN (
    'GENESIS', 'DEPOSIT', 'WITHDRAWAL', 'TRANSFER', 'HOLD',
    'HOLD_APPROVED', 'HOLD_REJECTED', 'REVERSAL',
    'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
    'USER_CREATED', 'USER_STATUS', 'AI_NOTE', 'ACCESS_DECISION'));
ALTER TABLE journal DROP CONSTRAINT event_kinds_are_info;
ALTER TABLE journal ADD CONSTRAINT event_kinds_are_info CHECK (
    (kind IN ('GENESIS', 'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
              'USER_CREATED', 'USER_STATUS', 'AI_NOTE', 'ACCESS_DECISION')) = (status = 'info'));

-- -----------------------------------------------------------------------------
-- Access requests: a request is not a user. It cannot sign in, read or move
-- anything. An administrator's approval creates the user; a decline does not.
-- -----------------------------------------------------------------------------
CREATE TABLE access_requests (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'),
    display_name  TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    role          TEXT NOT NULL CHECK (role = 'auditor'),
    -- Kept while the request waits, and after a decline (so the requester can be
    -- told the outcome when they sign in). On approval it moves into users: the
    -- same secret is never stored twice.
    password_hash TEXT,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'declined')),
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    decided_by    BIGINT REFERENCES users(id),
    decided_at    TIMESTAMPTZ,
    decision_note TEXT CHECK (decision_note IS NULL OR length(decision_note) <= 140),
    decision_seq  BIGINT UNIQUE REFERENCES journal(seq),
    user_id       BIGINT UNIQUE REFERENCES users(id),

    CONSTRAINT decided_means_recorded CHECK (
        (status = 'pending') = (decided_by IS NULL AND decided_at IS NULL AND decision_seq IS NULL)),
    CONSTRAINT approved_means_user CHECK ((status = 'approved') = (user_id IS NOT NULL)),
    CONSTRAINT secret_kept_once CHECK ((status = 'approved') = (password_hash IS NULL))
);
-- While a request waits, its username is reserved: one open request per name.
-- (Names are also checked against users, under a per-name lock, in admin_ops.)
CREATE UNIQUE INDEX access_requests_one_pending ON access_requests (username) WHERE status = 'pending';
CREATE INDEX access_requests_by_name ON access_requests (username, id);

CREATE OR REPLACE FUNCTION access_requests_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'an access request starts out pending' USING ERRCODE = 'LL012';
        END IF;
        RETURN NEW;
    END IF;

    -- A decision is final.
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'access request % was already %: decisions are final', OLD.id, OLD.status
            USING ERRCODE = 'LL012';
    END IF;
    -- What was asked for cannot be changed while it waits.
    IF NEW.id <> OLD.id OR NEW.username <> OLD.username OR NEW.display_name <> OLD.display_name
       OR NEW.role <> OLD.role OR NEW.requested_at <> OLD.requested_at THEN
        RAISE EXCEPTION 'access request % can be decided, not altered', OLD.id USING ERRCODE = 'LL012';
    END IF;
    -- The only change allowed is the decision itself, and it must be on the
    -- record: a signed ACCESS_DECISION entry for this request, with this
    -- outcome, written by the administrator named as the decider.
    IF NOT EXISTS (
        SELECT 1
          FROM journal j
          JOIN users decider ON decider.id = j.actor_id
         WHERE j.seq = NEW.decision_seq
           AND j.kind = 'ACCESS_DECISION'
           AND j.details->>'request_id' = NEW.id::text
           AND j.details->>'decision' = NEW.status
           AND j.actor_id = NEW.decided_by
           AND decider.role = 'admin') THEN
        RAISE EXCEPTION 'access request % can only be decided by an administrator, on the record', OLD.id
            USING ERRCODE = 'LL012';
    END IF;
    IF NEW.status = 'approved' AND NOT EXISTS (
        SELECT 1 FROM users u WHERE u.id = NEW.user_id AND u.username = NEW.username AND u.role = NEW.role) THEN
        RAISE EXCEPTION 'approved request % must point at the % it created', OLD.id, NEW.role
            USING ERRCODE = 'LL012';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER access_requests_guard BEFORE INSERT OR UPDATE ON access_requests
    FOR EACH ROW EXECUTE FUNCTION access_requests_guard();
CREATE TRIGGER access_requests_no_delete BEFORE DELETE ON access_requests
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER access_requests_no_truncate BEFORE TRUNCATE ON access_requests
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- -----------------------------------------------------------------------------
-- Users keep the role they were created with. Changing what someone may do
-- means a new user, created on the record - never a silent UPDATE.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION users_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.role <> OLD.role THEN
        RAISE EXCEPTION 'user % keeps the role it was created with (%)', OLD.username, OLD.role
            USING ERRCODE = 'LL013';
    END IF;
    IF NEW.id <> OLD.id OR NEW.username <> OLD.username OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'identity of user % is immutable', OLD.username USING ERRCODE = 'LL013';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER users_guard BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION users_guard();

-- Checked at COMMIT: every user created in this transaction has its signed
-- USER_CREATED entry, with the same role. Holders may sign themselves up;
-- auditors and administrators come from ANOTHER administrator, or from the
-- server's own command line (actor NULL: the first administrator, the seed).
CREATE OR REPLACE FUNCTION users_created_on_record() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM journal j
          LEFT JOIN users creator ON creator.id = j.actor_id
         WHERE j.kind = 'USER_CREATED'
           AND j.txid = txid_current()
           AND j.details->>'user_id' = NEW.id::text
           AND j.details->>'role' = NEW.role
           AND (NEW.role = 'holder'
                OR j.actor_id IS NULL
                OR (creator.role = 'admin' AND creator.id <> NEW.id))) THEN
        RAISE EXCEPTION 'user % (%) was not created on the record by an administrator', NEW.username, NEW.role
            USING ERRCODE = 'LL014';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER users_created_on_record AFTER INSERT ON users
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION users_created_on_record();

INSERT INTO schema_version (version) VALUES (2);
