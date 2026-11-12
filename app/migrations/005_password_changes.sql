-- =============================================================================
-- LedgerLine schema version 5: changing your own password.
--
-- A signed-in person changes their own password by giving the current one.
-- Every other session ends at once (the token version moves on). The change is
-- on the record as a PASSWORD_CHANGED entry: who and when, never the password
-- or its hash. Enforced HERE as well as in Python:
--
--   users_password_on_record (LL018) . at COMMIT, a new password hash needs a
--                                      PASSWORD_CHANGED entry written by that same
--                                      person in the same transaction. Nobody
--                                      changes someone else's password through
--                                      the application, and no change is silent.
-- =============================================================================

SET search_path TO ledgerline;

ALTER TABLE journal DROP CONSTRAINT journal_kind_check;
ALTER TABLE journal ADD CONSTRAINT journal_kind_check CHECK (kind IN (
    'GENESIS', 'DEPOSIT', 'WITHDRAWAL', 'TRANSFER', 'HOLD',
    'HOLD_APPROVED', 'HOLD_REJECTED', 'REVERSAL',
    'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
    'USER_CREATED', 'USER_STATUS', 'AI_NOTE', 'ACCESS_DECISION', 'MAIN_ADMIN',
    'DETAILS_SUBMITTED', 'DETAILS_VERIFIED', 'DETAILS_RETURNED', 'PASSWORD_CHANGED'));
ALTER TABLE journal DROP CONSTRAINT event_kinds_are_info;
ALTER TABLE journal ADD CONSTRAINT event_kinds_are_info CHECK (
    (kind IN ('GENESIS', 'ACCOUNT_OPENED', 'ACCOUNT_STATUS', 'OVERDRAFT_LIMIT',
              'USER_CREATED', 'USER_STATUS', 'AI_NOTE', 'ACCESS_DECISION', 'MAIN_ADMIN',
              'DETAILS_SUBMITTED', 'DETAILS_VERIFIED', 'DETAILS_RETURNED', 'PASSWORD_CHANGED')) = (status = 'info'));

CREATE OR REPLACE FUNCTION users_password_on_record() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM journal j
         WHERE j.kind = 'PASSWORD_CHANGED'
           AND j.txid = txid_current()
           AND j.details->>'user_id' = NEW.id::text
           AND j.actor_id = NEW.id) THEN
        RAISE EXCEPTION 'the password of % changed off the record, or not by its owner', NEW.username
            USING ERRCODE = 'LL018';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER users_password_on_record AFTER UPDATE OF password_hash ON users
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (OLD.password_hash IS DISTINCT FROM NEW.password_hash)
    EXECUTE FUNCTION users_password_on_record();

INSERT INTO schema_version (version) VALUES (5);
