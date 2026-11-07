"""Sentinel: the AI review layer. It flags and explains; it never moves money.

Architecture rule enforced by tests/test_ai_guardrails.py: nothing in this
package may import app.ledger.engine (the only code that moves money).
"""
