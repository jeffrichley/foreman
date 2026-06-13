"""Concrete observers — one file per observer.

Observers consume events from foreman.v4.event_bus.EventBus. They are
registered at daemon startup; their __call__ receives one Event per fire.
"""
