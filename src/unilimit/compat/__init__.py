"""Compatibility drop-in shims for legacy rate-limiting libraries (slowapi, django-ratelimit).

These adapters let AI agents and existing codebases migrate to UniLimit's robust
atomic Lua engine without rewriting route signatures or application setup.
"""

from __future__ import annotations

__all__ = ["django_ratelimit", "slowapi"]
