-- =============================================================================
-- LedgerLine schema version 3: the main administrator.
--
-- Day-to-day work (deposits, approvals, reviews, auditor requests) can be done
-- by any administrator, so the work does not stop when one person is away.
-- Who IS an administrator is decided by one person: the main administrator.
-- Enforced HERE as well as in Python:
--
--   users.is_main_admin ............. at most one, and only an administrator can be it
--   users_guard (LL013, LL015) ...... the flag never changes after creation, and the
--                                     main administrator's access can never be removed
--   users_created_on_record (LL014) . an administrator is created on the record by the
--                                     main administrator; only the main administrator
--                                     itself comes from the server's own command line
--   users_admin_access (LL016) ...... at COMMIT, removing or restoring an administrator's
--                                     access needs a signed USER_STATUS entry by the main
--                                     administrator
--
-- On an existing database, the first active administrator becomes the main one,
-- and `migrate` puts that on the signed record: one MAIN_ADMIN entry naming the
-- main administrator. Administrators created before that entry were made under
-- the earlier rule (any administrator could create one); the proof judges each
-- administrator by the rule in force when it was created.
-- =============================================================================

SET search_path TO ledgerline;

ALTER TABLE journal DROP CONSTRAINT journal_kind_check;
ALTER TABLE journal ADD CONSTRAINT journal_kind_check CHECK (kind IN (
    'GENESIS', 'DEPOSIT', 'WITHDRAWAL', 'TRANSFER', 'HOLD',
    'HOLD_APPROVED', 'HOLD_REJECTED', 'REVERSAL',
    'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
    'USER_CREATED', 'USER_STATUS', 'AI_NOTE', 'ACCESS_DECISION', 'MAIN_ADMIN'));
ALTER TABLE journal DROP CONSTRAINT event_kinds_are_info;
ALTER TABLE journal ADD CONSTRAINT event_kinds_are_info CHECK (
    (kind IN ('GENESIS', 'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
              'USER_CREATED', 'USER_STATUS', 'AI_NOTE', 'ACCESS_DECISION', 'MAIN_ADMIN')) = (status = 'info'));
-- The switch to the new rule happens once.
CREATE UNIQUE INDEX journal_one_main_admin_switch ON journal ((true)) WHERE kind = 'MAIN_ADMIN';

ALTER TABLE users ADD COLUMN is_main_admin BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD CONSTRAINT main_admin_is_an_admin CHECK (NOT is_main_admin OR role = 'admin');
CREATE UNIQUE INDEX users_one_main_admin ON users ((true)) WHERE is_main_admin;

UPDATE users SET is_main_admin = true
 WHERE id = (SELECT min(id) FROM users WHERE role = 'admin' AND is_active);

-- -----------------------------------------------------------------------------
-- users_guard, version 3: what a user is never changes, and the main
-- administrator cannot be locked out (not even by themselves).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION users_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.role <> OLD.role THEN
        RAISE EXCEPTION 'user % keeps the role it was created with (%)', OLD.username, OLD.role
            USING ERRCODE = 'LL013';
    END IF;
    IF NEW.is_main_admin <> OLD.is_main_admin THEN
        RAISE EXCEPTION 'who the main administrator is cannot be changed' USING ERRCODE = 'LL013';
    END IF;
    IF NEW.id <> OLD.id OR NEW.username <> OLD.username OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'identity of user % is immutable', OLD.username USING ERRCODE = 'LL013';
    END IF;
    IF OLD.is_main_admin AND NOT NEW.is_active THEN
        RAISE EXCEPTION 'the main administrator''s access cannot be removed' USING ERRCODE = 'LL015';
    END IF;
    RETURN NEW;
END
$$;

-- -----------------------------------------------------------------------------
-- users_created_on_record, version 3 (the trigger itself stays from version 2):
-- holders may sign themselves up; auditors come from an administrator (or the
-- server's command line, for the seed); administrators come only from the main
-- administrator, and the main administrator only from the command line.
-- -----------------------------------------------------------------------------
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
           AND CASE NEW.role
                 WHEN 'holder' THEN true
                 WHEN 'auditor' THEN j.actor_id IS NULL OR (creator.role = 'admin' AND creator.id <> NEW.id)
                 ELSE (j.actor_id IS NULL AND NEW.is_main_admin)
                      OR (creator.is_main_admin AND creator.id <> NEW.id AND NOT NEW.is_main_admin)
               END) THEN
        RAISE EXCEPTION 'user % (%) was not created on the record by the right administrator', NEW.username, NEW.role
            USING ERRCODE = 'LL014';
    END IF;
    RETURN NULL;
END
$$;

-- -----------------------------------------------------------------------------
-- Removing or restoring an ADMINISTRATOR's access: only the main administrator,
-- on the record. (Holders' and auditors' access stays with any administrator.)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION users_admin_access_on_record() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM journal j
          JOIN users decider ON decider.id = j.actor_id
         WHERE j.kind = 'USER_STATUS'
           AND j.txid = txid_current()
           AND j.details->>'user_id' = NEW.id::text
           AND (j.details->>'active')::boolean = NEW.is_active
           AND decider.is_main_admin AND decider.id <> NEW.id) THEN
        RAISE EXCEPTION 'only the main administrator can remove or restore administrator %, on the record', NEW.username
            USING ERRCODE = 'LL016';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER users_admin_access AFTER UPDATE OF is_active ON users
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.role = 'admin' AND OLD.is_active IS DISTINCT FROM NEW.is_active)
    EXECUTE FUNCTION users_admin_access_on_record();

INSERT INTO schema_version (version) VALUES (3);
