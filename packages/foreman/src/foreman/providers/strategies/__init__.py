"""Concrete recovery strategies for known SDK bug shapes (foreman#266).

Each strategy lives in its own module so failures localize and new
strategies can be added without touching the existing ones. The
deployed registration order is in
:func:`foreman.providers.make_provider`.
"""

from foreman.providers.strategies.success_as_error import SuccessAsErrorRecovery

__all__ = ["SuccessAsErrorRecovery"]
