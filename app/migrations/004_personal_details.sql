-- =============================================================================
-- LedgerLine schema version 4: personal details.
--
-- Every user can keep the basic details a bank needs: date of birth, mobile,
-- email, address, occupation and an optional nominee. No ID numbers. Only the
-- person changes their own details; any administrator except that person
-- checks them and verifies them or sends them back with a note.
--
-- The details never enter the journal. The journal is permanent and signed, and
-- personal details must stay correctable, so it records only WHO did WHAT to
-- WHICH version (and the names of the fields that changed), never a value.
-- Enforced HERE as well as in Python:
--
--   personal_details CHECKs ............. formats (mobile, PIN code, email...),
--                                         and nobody decides on their own details
--   personal_details_guard (LL017) ...... a change is a new version, waiting to be
--                                         checked again; a decision is made once per
--                                         version; details are never deleted (LL010)
--   personal_details_on_record (LL017) . at COMMIT, every change needs a signed
--                                         DETAILS_SUBMITTED entry by the person, and
--                                         every decision a DETAILS_VERIFIED or
--                                         DETAILS_RETURNED entry by another administrator
-- =============================================================================

SET search_path TO ledgerline;

ALTER TABLE journal DROP CONSTRAINT journal_kind_check;
ALTER TABLE journal ADD CONSTRAINT journal_kind_check CHECK (kind IN (
    'GENESIS', 'DEPOSIT', 'WITHDRAWAL', 'TRANSFER', 'HOLD',
    'HOLD_APPROVED', 'HOLD_REJECTED', 'REVERSAL',
    'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
    'USER_CREATED', 'USER_STATUS', 'AI_NOTE', 'ACCESS_DECISION', 'MAIN_ADMIN',
    'DETAILS_SUBMITTED', 'DETAILS_VERIFIED', 'DETAILS_RETURNED'));
ALTER TABLE journal DROP CONSTRAINT event_kinds_are_info;
ALTER TABLE journal ADD CONSTRAINT event_kinds_are_info CHECK (
    (kind IN ('GENESIS', 'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
              'USER_CREATED', 'USER_STATUS', 'AI_NOTE', 'ACCESS_DECISION', 'MAIN_ADMIN',
              'DETAILS_SUBMITTED', 'DETAILS_VERIFIED', 'DETAILS_RETURNED')) = (status = 'info'));

CREATE TABLE personal_details (
    user_id          BIGINT PRIMARY KEY REFERENCES users(id),
    date_of_birth    DATE NOT NULL CHECK (date_of_birth >= DATE '1900-01-01'),
    mobile           TEXT NOT NULL CHECK (mobile ~ '^[6-9][0-9]{9}$'),
    email            TEXT NOT NULL CHECK (length(email) <= 254
                                          AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'),
    address_line1    TEXT NOT NULL CHECK (length(address_line1) BETWEEN 1 AND 120),
    address_line2    TEXT CHECK (address_line2 IS NULL OR length(address_line2) BETWEEN 1 AND 120),
    city             TEXT NOT NULL CHECK (length(city) BETWEEN 1 AND 60),
    state            TEXT NOT NULL CHECK (length(state) BETWEEN 1 AND 60),
    pin_code         TEXT NOT NULL CHECK (pin_code ~ '^[1-9][0-9]{5}$'),
    occupation       TEXT NOT NULL CHECK (occupation IN ('salaried', 'self_employed', 'business', 'student',
                                                         'homemaker', 'retired', 'other')),
    nominee_name     TEXT CHECK (nominee_name IS NULL OR length(nominee_name) BETWEEN 1 AND 80),
    nominee_relation TEXT CHECK (nominee_relation IS NULL OR nominee_relation IN (
                                     'spouse', 'father', 'mother', 'son', 'daughter', 'brother', 'sister', 'other')),
    status           TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'verified', 'returned')),
    version          INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    submitted_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    decided_by       BIGINT REFERENCES users(id),
    decided_at       TIMESTAMPTZ,
    return_note      TEXT CHECK (return_note IS NULL OR length(return_note) BETWEEN 1 AND 200),
    CONSTRAINT nominee_complete CHECK ((nominee_name IS NULL) = (nominee_relation IS NULL)),
    CONSTRAINT decision_complete CHECK (
           (status = 'waiting'  AND decided_by IS NULL     AND decided_at IS NULL     AND return_note IS NULL)
        OR (status = 'verified' AND decided_by IS NOT NULL AND decided_at IS NOT NULL AND return_note IS NULL)
        OR (status = 'returned' AND decided_by IS NOT NULL AND decided_at IS NOT NULL AND return_note IS NOT NULL)),
    CONSTRAINT nobody_decides_their_own CHECK (decided_by IS NULL OR decided_by <> user_id)
);
CREATE INDEX personal_details_waiting ON personal_details (submitted_at) WHERE status = 'waiting';

-- Details are corrected by a new version, never deleted.
CREATE TRIGGER personal_details_no_delete BEFORE DELETE ON personal_details
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER personal_details_no_truncate BEFORE TRUNCATE ON personal_details
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- -----------------------------------------------------------------------------
-- A change is a new version that waits to be checked again (it never keeps a
-- "verified" made for an earlier version). A decision is made once per version.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION personal_details_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'waiting' OR NEW.version <> 1 THEN
            RAISE EXCEPTION 'new personal details start as version 1, waiting to be checked' USING ERRCODE = 'LL017';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.user_id <> OLD.user_id THEN
        RAISE EXCEPTION 'personal details cannot move to another user' USING ERRCODE = 'LL017';
    END IF;
    IF (NEW.date_of_birth, NEW.mobile, NEW.email, NEW.address_line1, NEW.address_line2, NEW.city, NEW.state,
        NEW.pin_code, NEW.occupation, NEW.nominee_name, NEW.nominee_relation)
       IS DISTINCT FROM
       (OLD.date_of_birth, OLD.mobile, OLD.email, OLD.address_line1, OLD.address_line2, OLD.city, OLD.state,
        OLD.pin_code, OLD.occupation, OLD.nominee_name, OLD.nominee_relation) THEN
        IF NEW.version <> OLD.version + 1 OR NEW.status <> 'waiting' THEN
            RAISE EXCEPTION 'changed personal details of user % are a new version that must be checked again',
                OLD.user_id USING ERRCODE = 'LL017';
        END IF;
    ELSIF NEW.version <> OLD.version THEN
        RAISE EXCEPTION 'a new version of personal details needs a change' USING ERRCODE = 'LL017';
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status <> 'waiting' OR NEW.status = 'waiting' THEN
            RAISE EXCEPTION 'version % of the personal details of user % was already %', OLD.version, OLD.user_id,
                OLD.status USING ERRCODE = 'LL017';
        END IF;
    ELSIF ROW(NEW.*) IS DISTINCT FROM ROW(OLD.*) THEN
        RAISE EXCEPTION 'personal details change only by a new version or a decision' USING ERRCODE = 'LL017';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER personal_details_guard BEFORE INSERT OR UPDATE ON personal_details
    FOR EACH ROW EXECUTE FUNCTION personal_details_guard();

-- -----------------------------------------------------------------------------
-- At COMMIT: who did it must be on the signed record. A new version needs a
-- DETAILS_SUBMITTED entry by the person themselves; a decision needs a
-- DETAILS_VERIFIED / DETAILS_RETURNED entry by an administrator who is not that
-- person, for that exact version.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION personal_details_on_record() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    expected TEXT;
BEGIN
    IF TG_OP = 'INSERT' OR NEW.version <> OLD.version THEN
        expected := 'DETAILS_SUBMITTED';
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        expected := CASE NEW.status WHEN 'verified' THEN 'DETAILS_VERIFIED' ELSE 'DETAILS_RETURNED' END;
    ELSE
        RETURN NULL;           -- nothing changed (the guard refuses every other kind of update)
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM journal j
          JOIN users actor ON actor.id = j.actor_id
         WHERE j.kind = expected
           AND j.txid = txid_current()
           AND j.details->>'user_id' = NEW.user_id::text
           AND j.details->>'version' = NEW.version::text
           AND CASE WHEN expected = 'DETAILS_SUBMITTED' THEN actor.id = NEW.user_id
                    ELSE actor.role = 'admin' AND actor.id <> NEW.user_id AND actor.id = NEW.decided_by END) THEN
        RAISE EXCEPTION 'personal details of user % (version %) changed off the record or by the wrong person',
            NEW.user_id, NEW.version USING ERRCODE = 'LL017';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER personal_details_on_record AFTER INSERT OR UPDATE ON personal_details
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION personal_details_on_record();

INSERT INTO schema_version (version) VALUES (4);
