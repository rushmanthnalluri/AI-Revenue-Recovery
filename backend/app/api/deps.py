"""Shared FastAPI dependency seams for the API layer.

`get_gateway_dependency` is THE single gateway seam: routers declare it with
`Depends(...)` and tests override the one function object via
`app.dependency_overrides[get_gateway_dependency]`. It used to be duplicated
in `api/v1/webhooks.py` and `api/v1/recovery.py` — two distinct function
objects meant overriding one seam silently left the other bound to the
environment gateway.

`get_principal` is the KYA-lite seam (docs/security-testing.md): it resolves
an authenticated-lite Principal from the X-API-Key the ApiKeyMiddleware
already verified, so mutating recovery calls carry a key-derived identity in
addition to the self-declared actor string. This is DEMO-GRADE identity —
a shared static key identifies a cohort, not a person; it is not SSO, OIDC,
or per-user authN/Z.
"""

from dataclasses import dataclass

from fastapi import Header

from app.config import settings
from app.ports import PaymentGateway
from app.services.razorpay.factory import get_gateway


def get_gateway_dependency() -> PaymentGateway:
    """FastAPI dependency seam — tests override this with a fixed gateway."""
    return get_gateway(settings)


# ---------------------------------------------------------------------------
# KYA-lite principal binding (demo-grade — see docs/security-testing.md)
# ---------------------------------------------------------------------------

# Known API keys -> stable principal ids. The ApiKeyMiddleware (app.main)
# authenticates the key itself; this table only gives an already-authenticated
# key a durable, greppable identity for the audit trail. Key MATERIAL is never
# logged or persisted — only the principal id is.
#
# Demo posture: the single shared dev key maps to the DEFAULT cohort
# principal, so actor strings pass through unattributed (the shared key adds
# no per-caller discrimination, and rewriting every actor would needlessly
# churn the existing audit surface). A deployment that issues per-operator
# keys adds them here and gets `actor@kya:<principal>` attribution on every
# mutating call automatically.
PRINCIPAL_BY_API_KEY: dict[str, str] = {
    # settings.API_KEY default — the shared demo operator cohort.
    "dev-key": "demo-operator",
}

# Principal id for callers whose key is absent or not in the table. The
# middleware rejects unknown keys on mutating routes before this runs, so an
# unauthenticated principal here means a read-only/exempt path or a test seam.
UNAUTHENTICATED_PRINCIPAL = "unauthenticated"

# The cohort principal that leaves actor strings untouched (see above).
DEFAULT_PRINCIPAL = "demo-operator"

# Attribution marker inside actor strings, e.g. "human:ops@kya:ops-lead".
# Deliberately NOT a bare "@" suffix: existing actor values are email-shaped
# ("human:ops@pulserecover.demo"), so an explicit scheme makes principal
# extraction unambiguous and keeps emails from misparsing as principals.
ATTRIBUTION_MARKER = "@kya:"


@dataclass(frozen=True)
class Principal:
    """Authenticated-lite identity of a mutating caller (KYA-lite).

    Derived from the presented X-API-Key (already authenticated by the
    middleware) and combined with the request's self-declared actor field at
    the call site. NOT a per-user identity: the demo deployment has one
    shared key, so the principal identifies a cohort.
    """

    id: str

    @property
    def authenticated(self) -> bool:
        return self.id != UNAUTHENTICATED_PRINCIPAL

    def attributed_actor(self, actor: str) -> str:
        """The actor string stamped with this principal.

        Pass-through (unchanged) for the default cohort principal and for
        unauthenticated callers; non-default principals attribute as
        ``"<actor>@kya:<principal-id>"`` — the executor, the audit trail, and
        `approved_by` then carry the authenticated principal alongside the
        self-declared actor.
        """
        if not self.authenticated or self.id == DEFAULT_PRINCIPAL:
            return actor
        return f"{actor}{ATTRIBUTION_MARKER}{self.id}"

    @staticmethod
    def principal_of_actor(actor: str | None) -> str | None:
        """The principal id carried by an attributed actor string, else None
        (unattributed actors — the norm under the shared demo key)."""
        if not actor or ATTRIBUTION_MARKER not in actor:
            return None
        suffix = actor.rsplit(ATTRIBUTION_MARKER, 1)[1].strip()
        return suffix or None


def get_principal(x_api_key: str | None = Header(default=None)) -> Principal:
    """FastAPI dependency: resolve the KYA-lite principal for this call.

    Reads the X-API-Key the ApiKeyMiddleware already authenticated; unknown
    or absent keys yield the unauthenticated principal. Never raises — the
    middleware owns rejection, this dependency owns attribution.
    """
    if x_api_key:
        principal_id = PRINCIPAL_BY_API_KEY.get(x_api_key)
        if principal_id:
            return Principal(principal_id)
    return Principal(UNAUTHENTICATED_PRINCIPAL)


__all__ = [
    "ATTRIBUTION_MARKER",
    "DEFAULT_PRINCIPAL",
    "PRINCIPAL_BY_API_KEY",
    "Principal",
    "UNAUTHENTICATED_PRINCIPAL",
    "get_gateway_dependency",
    "get_principal",
]
