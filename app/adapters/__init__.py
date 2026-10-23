"""Sponsor service adapters (Zetaris, Meterless). Each one has timeouts, bounded
retries and raises AdapterError instead of leaking raw exceptions."""

from app.adapters.http import AdapterError

__all__ = ["AdapterError"]
