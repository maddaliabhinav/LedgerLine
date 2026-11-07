"""Who may move which money, decided by the server on every request.
Every refusal is recorded in the journal, on the targeted account's statement."""

from __future__ import annotations

from app import db
from app.ledger import accounts, engine, statements
from tests.helpers import (RUPEE, admin, assert_books_balance, balance, entries, key, make_user, open_account)


def refused_code(outcome) -> str:
    assert outcome.body["status"] == "refused", outcome.body
    return outcome.body["error"]["code"]


def statement_rows(account_no: str) -> list[dict]:
    with db.connect() as conn:
        cur = conn.cursor()
        return statements.statement(cur, accounts.find(cur, account_no))


def test_holder_cannot_withdraw_from_someone_elses_account():
    victim, thief = make_user(), make_user()
    target = open_account(victim, 1000)
    out = engine.withdraw(thief, target, 500 * RUPEE, "mine now", key())
    assert refused_code(out) == "NOT_ACCOUNT_OWNER"
    assert out.http_status == 403
    assert "balances" not in out.body                       # the thief learns nothing about the balance
    assert balance(target) == 1000 * RUPEE
    row = statement_rows(target)[0]                          # the victim sees the attempt, and who made it
    assert row["status"] == "refused" and row["reason_code"] == "NOT_ACCOUNT_OWNER"
    assert row["initiated_by"]["username"] == thief.username
    assert row["balance_after"]["paise"] == 1000 * RUPEE
    assert_books_balance()


def test_holder_cannot_transfer_out_of_someone_elses_account():
    victim, thief = make_user(), make_user()
    target, loot = open_account(victim, 1000), open_account(thief, 0)
    out = engine.transfer(thief, target, loot, 999 * RUPEE, "vendor payment", key())
    assert refused_code(out) == "NOT_ACCOUNT_OWNER"
    assert balance(target) == 1000 * RUPEE and balance(loot) == 0
    assert_books_balance()


def test_holder_cannot_mint_money_with_a_deposit():
    holder = make_user()
    acct = open_account(holder, 0)
    out = engine.deposit(holder, acct, 1_00_000 * RUPEE, "free money", key())
    assert refused_code(out) == "ROLE_NOT_PERMITTED"
    assert balance(acct) == 0
    assert_books_balance()


def test_auditor_is_read_only():
    auditor, holder = make_user("auditor"), make_user()
    acct, other = open_account(holder, 1000), open_account(make_user(), 0)
    assert refused_code(engine.deposit(auditor, acct, 100 * RUPEE, None, key())) == "ROLE_NOT_PERMITTED"
    assert refused_code(engine.withdraw(auditor, acct, 100 * RUPEE, None, key())) == "ROLE_NOT_PERMITTED"
    assert refused_code(engine.transfer(auditor, acct, other, 100 * RUPEE, None, key())) == "ROLE_NOT_PERMITTED"
    assert balance(acct) == 1000 * RUPEE
    assert_books_balance()


def test_admin_cannot_take_money_out_of_a_customer_account():
    holder = make_user()
    acct, other = open_account(holder, 1000), open_account(make_user(), 0)
    assert refused_code(engine.withdraw(admin(), acct, 100 * RUPEE, None, key())) == "ROLE_NOT_PERMITTED"
    assert refused_code(engine.transfer(admin(), acct, other, 100 * RUPEE, None, key())) == "ROLE_NOT_PERMITTED"
    assert balance(acct) == 1000 * RUPEE


def test_only_an_admin_can_reverse():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    paid = engine.transfer(a, src, dst, 100 * RUPEE, "x", key())
    assert refused_code(engine.reverse(a, paid.entry_seq, "I changed my mind", key())) == "ROLE_NOT_PERMITTED"
    assert refused_code(engine.reverse(b, paid.entry_seq, "give it back", key())) == "ROLE_NOT_PERMITTED"
    assert balance(dst) == 100 * RUPEE


def test_money_cannot_be_sent_to_system_accounts():
    a = make_user()
    src = open_account(a, 1000)
    out = engine.transfer(a, src, "SYS-SUSPENSE", 100 * RUPEE, "sneaky", key())
    assert refused_code(out) == "DESTINATION_NOT_ALLOWED"
    out = engine.transfer(a, src, "SYS-EXTERNAL", 100 * RUPEE, "sneaky", key())
    assert refused_code(out) == "DESTINATION_NOT_ALLOWED"
    assert balance(src) == 1000 * RUPEE


def test_frozen_accounts_neither_send_nor_receive():
    from app.ledger import admin_ops
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 1000)
    admin_ops.set_status(admin().id, dst, "frozen", "investigation")
    assert refused_code(engine.transfer(a, src, dst, 10 * RUPEE, None, key())) == "DESTINATION_NOT_ACTIVE"
    assert refused_code(engine.withdraw(b, dst, 10 * RUPEE, None, key())) == "ACCOUNT_NOT_ACTIVE"
    assert refused_code(engine.deposit(admin(), dst, 10 * RUPEE, None, key())) == "ACCOUNT_NOT_ACTIVE"
    assert balance(src) == 1000 * RUPEE and balance(dst) == 1000 * RUPEE


def test_same_account_and_missing_destination_are_refused_and_recorded():
    a = make_user()
    src = open_account(a, 1000)
    assert refused_code(engine.transfer(a, src, src, 10 * RUPEE, None, key())) == "SAME_ACCOUNT"
    assert refused_code(engine.transfer(a, src, "LL-99999999", 10 * RUPEE, None, key())) == "DESTINATION_NOT_FOUND"
    assert len(entries("status = 'refused' AND account_id = (SELECT id FROM accounts WHERE account_no = %s)",
                       (src,))) == 2
    assert balance(src) == 1000 * RUPEE


def test_insufficient_funds_is_refused_by_the_application_check():
    """Single request, no race: the application must refuse it itself
    (not leave it to the database backstop)."""
    a, b = make_user(), make_user()
    src, dst = open_account(a, 100), open_account(b, 0)
    assert refused_code(engine.transfer(a, src, dst, 101 * RUPEE, None, key())) == "INSUFFICIENT_FUNDS"
    assert refused_code(engine.withdraw(a, src, 101 * RUPEE, None, key())) == "INSUFFICIENT_FUNDS"
    assert balance(src) == 100 * RUPEE
    assert_books_balance()
