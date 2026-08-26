"""Pydantic v2 request/response schemas — the frontend contract.

Money is integer paise (`*_paise`) plus `currency: "INR"`. Datetimes are
ISO-8601 UTC. One module per API domain; routers in app.api.v1 import from
here only.
"""
