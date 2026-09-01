"""Audit-trail integrity services.

`verify_chain` walks the hash-chained `audit_logs` table (chained at insert
time by the model-layer hook in app.models.system) and reports tampering,
gaps, and unhashed rows. Read-only: verification never writes.
"""

from app.services.audit.verify import ChainVerification, verify_chain

__all__ = ["ChainVerification", "verify_chain"]
