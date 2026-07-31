"""Partial redaction helpers for safely sharing notebook output.

Designed for screenshots / pasted logs where the reader still needs enough of
each identifier to recognise it (first/last few chars), but not enough to
re-derive the secret. Pure stdlib, no dependencies.

Typical use:

    from utils.redact import redact, redact_text, mask_ip, mask_guid

    print(f"Subscription : {mask_guid(settings.subscription_id)}")
    print(f"Client IPv4  : {mask_ip(my_ip)}")
    print(redact_text(some_multiline_output))

`redact_text(s)` runs the most common patterns (GUIDs, IPv4, ARM resource ids,
SAS tokens) over arbitrary text — handy for `print(redact_text(repr(obj)))`.
"""

from __future__ import annotations

import re
from typing import Iterable

__all__ = [
    "mask_guid",
    "mask_ip",
    "mask_ip_in_name",
    "mask_arm_id",
    "mask_sas_token",
    "redact",
    "redact_text",
]

_GUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_IPV4_RE = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
# Same as IPv4 but with dashes — matches names like "client-83-171-168-0-24".
_IPV4_DASH_RE = re.compile(r"\b(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})(?:-\d{1,3})?\b")
_SAS_TOKEN_RE = re.compile(r"(\bsig=)[^&\s\"']+", re.IGNORECASE)
_ARM_SUB_RE = re.compile(
    r"(/subscriptions/)([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)


def mask_guid(value: str, keep: int = 4) -> str:
    """Mask a GUID, keeping `keep` chars at the start and end.

    >>> mask_guid("01234567-89ab-cdef-0123-456789abcdef")
    '0123…cdef'
    """
    if not value:
        return value
    if len(value) <= 2 * keep:
        return "…" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def mask_ip(value: str) -> str:
    """Mask an IPv4 address: keep first octet, mask the rest.

    >>> mask_ip("10.20.30.40")
    '10.x.x.x'
    """
    parts = (value or "").split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return value
    return f"{parts[0]}.x.x.x"


def mask_ip_in_name(name: str) -> str:
    """Mask dashed-IP segments inside a resource/rule name.

    >>> mask_ip_in_name("client-10-20-30-0-24")
    'client-10-x-x-x-24'
    """
    if not name:
        return name

    def _sub(m: re.Match) -> str:
        rest = m.group(0)[len(m.group(1)) + 1:]  # everything after the first octet+'-'
        # Re-construct: first octet kept, middle two masked, last octet (+optional prefix) preserved.
        # rest can be like "171-168-0" or "171-168-0-24"
        bits = rest.split("-")
        if len(bits) < 3:
            return m.group(0)
        # bits[0..2] are the masked octets; bits[3:] is the optional CIDR-prefix suffix
        kept_suffix = "-".join(bits[3:])
        out = f"{m.group(1)}-x-x-x"
        if kept_suffix:
            out += f"-{kept_suffix}"
        return out

    return _IPV4_DASH_RE.sub(_sub, name)


def mask_arm_id(arm_id: str) -> str:
    """Mask the subscription GUID inside an ARM resource id.

    >>> mask_arm_id("/subscriptions/01234567-89ab-cdef-0123-456789abcdef/resourceGroups/rg-x/...")
    '/subscriptions/0123…cdef/resourceGroups/rg-x/...'
    """
    if not arm_id:
        return arm_id
    return _ARM_SUB_RE.sub(lambda m: m.group(1) + mask_guid(m.group(2)), arm_id)


def mask_sas_token(url: str) -> str:
    """Strip the signature portion of a SAS URL query string.

    >>> mask_sas_token("https://x.blob.core.windows.net/c?sv=2024-08-04&sig=ABCDEF...")
    'https://x.blob.core.windows.net/c?sv=2024-08-04&sig=…REDACTED…'
    """
    if not url:
        return url
    return _SAS_TOKEN_RE.sub(r"\1…REDACTED…", url)


def redact_text(text: str) -> str:
    """Apply all standard redactions to free-form text.

    Order matters: ARM-id subscription mask runs before the generic GUID mask
    so the path prefix is preserved. Dashed-IP names are masked before raw
    IPv4 so `client-83-171-168-0-24` doesn't get partially mangled.
    """
    if not text:
        return text
    s = mask_arm_id(text)
    # Generic GUIDs that weren't part of an ARM id
    s = _GUID_RE.sub(lambda m: mask_guid(m.group(0)), s)
    # Dashed IPv4 inside names (e.g. NSP rule names)
    s = _IPV4_DASH_RE.sub(
        lambda m: mask_ip_in_name(m.group(0)), s
    )
    # Plain IPv4
    s = _IPV4_RE.sub(lambda m: mask_ip(m.group(0)), s)
    # SAS signatures
    s = mask_sas_token(s)
    return s


def redact(*values: object) -> tuple[str, ...] | str:
    """Convenience: redact one or more values; returns a single str if one arg.

    >>> redact("01234567-89ab-cdef-0123-456789abcdef")
    '0123…cdef'
    >>> redact("10.20.30.40", "/subscriptions/01234567-89ab-cdef-0123-456789abcdef/rg/x")
    ('10.x.x.x', '/subscriptions/0123…cdef/rg/x')
    """
    out = tuple(redact_text(str(v)) for v in values)
    return out[0] if len(out) == 1 else out
