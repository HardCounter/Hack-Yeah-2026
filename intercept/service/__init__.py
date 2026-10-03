"""Loopback HTTP services for the OpenCode adapter.

- `server`    bounded HTTP transport; standalone policy gateway (`python -m intercept.service.server`)
- `local`     the same transport over the governed runtime (`python -m intercept.service.local`)
- `receiver`  observe-only diagnostic server, logs and ALLOWs (`python -m intercept.service.receiver`)
"""
