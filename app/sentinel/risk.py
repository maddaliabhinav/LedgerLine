"""Deterministic, explainable risk scoring for outgoing money.

Why rules + robust statistics rather than a black-box model:
* A reviewer must see WHY something was held, in plain words, with numbers.
* The same input always gives the same score, so it can be tested and audited.
* It runs in a few milliseconds inside the request, with no network call.

The score is a suggestion. A transfer at or above HOLD_THRESHOLD is not paid:
its money is parked in SYS-SUSPENSE and a person approves or rejects it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from psycopg import Cursor

from ..money import format_inr

RULESET = "sentinel-rules-v1"
HOLD_THRESHOLD = 60

RUPEE = 100
LARGE = 1_00_000 * RUPEE          # Rs 1,00,000
VERY_LARGE = 5_00_000 * RUPEE     # Rs 5,00,000
MEANINGFUL = 25_000 * RUPEE       # below this, soft signals are ignored
DRAIN_MIN = 10_000 * RUPEE


@dataclass(frozen=True)
class Reason:
    code: str
    points: int
    text: str


@dataclass(frozen=True)
class Assessment:
    score: int
    hold: bool
    reasons: tuple[Reason, ...]
    features: dict

    def record(self) -> dict:
        """What goes into the journal entry (the AI suggestion, on the record)."""
        return {
            "ruleset": RULESET,
            "score": self.score,
            "threshold": HOLD_THRESHOLD,
            "held": self.hold,
            "reasons": [{"code": r.code, "points": r.points, "text": r.text} for r in self.reasons],
            "features": self.features,
        }


def _features(cur: Cursor, source: dict, destination: dict | None, amount: int, actor_id: int) -> dict:
    cur.execute(
        """SELECT -p.amount AS amount
             FROM postings p JOIN journal j ON j.seq = p.entry_seq
            WHERE p.account_id = %s AND p.amount < 0
              AND j.kind IN ('TRANSFER', 'WITHDRAWAL', 'HOLD')
            ORDER BY p.entry_seq DESC LIMIT 50""",
        (source["id"],),
    )
    history = [row["amount"] for row in cur.fetchall()]
    median = int(statistics.median(history)) if history else 0
    if history:
        mad = statistics.median([abs(x - median) for x in history])
        spread = 1.4826 * mad if mad > 0 else max(median * 0.25, 100.0)
        z_x10 = int(round((amount - median) / spread * 10))
    else:
        z_x10 = 0

    paid_before = False
    if destination is not None:
        cur.execute(
            """SELECT EXISTS (SELECT 1 FROM journal
                               WHERE account_id = %s AND counterparty_id = %s AND status = 'posted'
                                 AND kind IN ('TRANSFER', 'HOLD_APPROVED')) AS paid""",
            (source["id"], destination["id"]),
        )
        paid_before = cur.fetchone()["paid"]

    cur.execute(
        """SELECT count(*) FILTER (WHERE created_at >= clock_timestamp() - interval '10 minutes') AS last10,
                  count(*) FILTER (WHERE amount >= %s AND amount < %s) AS near
             FROM journal
            WHERE account_id = %s AND status = 'posted'
              AND kind IN ('TRANSFER', 'WITHDRAWAL', 'HOLD')
              AND created_at >= clock_timestamp() - interval '24 hours'""",
        (LARGE * 8 // 10, LARGE, source["id"]),
    )
    window = cur.fetchone()

    cur.execute(
        """SELECT count(*) AS refused FROM journal
            WHERE actor_id = %s AND status = 'refused'
              AND created_at >= clock_timestamp() - interval '1 hour'""",
        (actor_id,),
    )
    refused = cur.fetchone()["refused"]

    cur.execute(
        """SELECT COALESCE(sum(p.amount), 0)::bigint AS inflow
             FROM postings p JOIN journal j ON j.seq = p.entry_seq
            WHERE p.account_id = %s AND p.amount > 0
              AND j.created_at >= clock_timestamp() - interval '1 hour'""",
        (source["id"],),
    )
    inflow = cur.fetchone()["inflow"]

    cur.execute(
        """SELECT extract(hour FROM clock_timestamp() AT TIME ZONE 'Asia/Kolkata')::int AS ist_hour,
                  CASE WHEN %s::timestamptz IS NULL THEN NULL
                       ELSE floor(extract(epoch FROM clock_timestamp() - %s::timestamptz) / 3600)::int
                  END AS dest_age_hours""",
        (destination["created_at"] if destination else None, destination["created_at"] if destination else None),
    )
    clock = cur.fetchone()

    return {
        "history_count": len(history),
        "median_outgoing": median,
        "robust_z_x10": z_x10,
        "available_before": source["balance"] + source["overdraft_limit"],
        "paid_before": paid_before,
        "outgoing_last_10_min": window["last10"],
        "near_threshold_24h": window["near"],
        "refused_last_hour": refused,
        "inflow_last_hour": inflow,
        "ist_hour": clock["ist_hour"],
        "destination_age_hours": clock["dest_age_hours"],
    }


def assess(cur: Cursor, *, kind: str, source: dict, destination: dict | None, amount: int,
           actor_id: int) -> Assessment:
    """Score an outgoing payment 0..100 with human-readable reasons."""
    f = _features(cur, source, destination, amount, actor_id)
    reasons: list[Reason] = []

    def add(code: str, points: int, text: str) -> None:
        reasons.append(Reason(code, points, text))

    to_customer = destination is not None and destination["kind"] == "customer"

    if amount >= VERY_LARGE:
        add("VERY_LARGE", 60, f"amount is at least {format_inr(VERY_LARGE)}")
    elif amount >= LARGE:
        add("LARGE", 40, f"amount is at least {format_inr(LARGE)}")

    if f["history_count"] >= 5:
        if f["robust_z_x10"] >= 60:
            add("UNUSUAL_FOR_ACCOUNT", 35,
                f"far above this account's typical outgoing payment of {format_inr(f['median_outgoing'])}")
        elif f["robust_z_x10"] >= 35:
            add("UNUSUAL_FOR_ACCOUNT", 15,
                f"well above this account's typical outgoing payment of {format_inr(f['median_outgoing'])}")

    if amount >= DRAIN_MIN and f["available_before"] > 0 and amount * 10 >= f["available_before"] * 9:
        pct = amount * 100 // f["available_before"]
        add("DRAINS_ACCOUNT", 20, f"moves {pct}% of the available balance")

    if to_customer and not f["paid_before"] and amount >= MEANINGFUL:
        add("NEW_PAYEE", 15, "first payment from this account to this payee")

    if f["outgoing_last_10_min"] >= 5:
        add("VELOCITY", 25, f"{f['outgoing_last_10_min']} outgoing payments in the last 10 minutes")

    if LARGE * 8 // 10 <= amount < LARGE and f["near_threshold_24h"] >= 2:
        add("STRUCTURING", 45,
            f"{f['near_threshold_24h'] + 1} payments just under {format_inr(LARGE)} within 24 hours")

    if f["refused_last_hour"] >= 3:
        add("PROBING", 20, f"{f['refused_last_hour']} refused attempts by this user in the last hour")

    if to_customer and f["destination_age_hours"] is not None and f["destination_age_hours"] < 24 \
            and amount >= MEANINGFUL:
        add("FRESH_DESTINATION", 15, "destination account was opened less than 24 hours ago")

    if amount >= MEANINGFUL and f["inflow_last_hour"] * 10 >= amount * 8:
        add("PASS_THROUGH", 20, "most of this amount arrived in the account within the last hour")

    if amount >= MEANINGFUL and f["ist_hour"] < 5:
        add("ODD_HOUR", 10, "initiated between midnight and 5 a.m. IST")

    score = min(100, sum(r.points for r in reasons))
    return Assessment(score=score, hold=score >= HOLD_THRESHOLD, reasons=tuple(reasons), features=f)
