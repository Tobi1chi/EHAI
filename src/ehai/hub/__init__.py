"""Agent harness Hub: the only surface through which the EHAI core runs harnesses.

The core imports only ``ehai.hub.protocol`` and ``ehai.hub.client``. The service
(``ehai.hub.server``) and the per-harness compatibility layers (``ehai.hub.adapters``)
never import core application, domain, infrastructure or interface modules. See
``docs/adr/0007-agent-harness-port.md`` and ``docs/HUB.md``.
"""
