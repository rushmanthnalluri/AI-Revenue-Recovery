"""Full-chain verification for the hash-chained audit trail.

The chain is written transparently by the `before_insert` hook on
`app.models.system.AuditLog`: every row's `previous_hash` is the `entry_hash`
of the previously inserted chained row, and `entry_hash` is sha256 over the
row's canonical fields (id, ts, actor, action, entity, details, previous_hash).

`verify_chain` replays that construction over the WHOLE table in chain order
(created_at, id — the same order the insert hook's head query uses):

- recompute each chained row's digest — a mismatch means the row's own fields
  were edited after insertion (tamper detected at that row);
- check each chained row's `previous_hash` equals the previous chained row's
  `entry_hash` — a mismatch means a gap (row deleted), a fork, or a
  recomputed-without-cascade edit (detected at the successor);
- rows with NULL hashes that precede chain genesis are pre-chain legacy rows
  and verify as legacy-valid; an unhashed row AFTER genesis (raw-SQL insert,
  stripped hash) is flagged.

Environment scoping deliberately does NOT apply: the chain spans the whole
table in insertion order (rows from both environments interleave), so
verifying only one environment would break linkage. The walk is read-only.

Honesty notes (also in docs/security-testing.md): this is tamper-EVIDENCE,
not tamper-proof — an attacker with full DB write access can recompute the
entire chain, and deletion of the current head row is undetectable without
an external anchor. Single-node assumption matches the insert hook's.
"""

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import AuditLog
from app.models.system import compute_entry_hash


@dataclass(frozen=True)
class ChainVerification:
    valid: bool
    checked: int  # rows examined, including the first bad row
    chained: int  # examined rows carrying hashes
    legacy: int  # examined pre-chain rows (NULL hashes) — legacy-valid
    first_bad_id: str | None


def verify_chain(session: Session) -> ChainVerification:
    """Walk the full audit table; return the verdict and the first bad row id."""
    rows = session.scalars(
        sa.select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    ).all()

    chained = 0
    legacy = 0
    prev_hash: str | None = None
    started = False
    for row in rows:
        examined = chained + legacy + 1
        if row.entry_hash is None:
            if started:
                # Unhashed row after chain genesis: bypassed the ORM hook or
                # had its hash stripped — the chain cannot vouch for it.
                return ChainVerification(False, examined, chained, legacy, row.id)
            legacy += 1
            continue
        started = True
        if row.previous_hash != prev_hash:
            # Link broken: gap/fork upstream, or a predecessor's hash was
            # recomputed without cascading to this row.
            return ChainVerification(False, examined, chained, legacy, row.id)
        if compute_entry_hash(row) != row.entry_hash:
            # This row's own fields were edited after insertion.
            return ChainVerification(False, examined, chained, legacy, row.id)
        prev_hash = row.entry_hash
        chained += 1

    return ChainVerification(True, chained + legacy, chained, legacy, None)


__all__ = ["ChainVerification", "verify_chain"]
