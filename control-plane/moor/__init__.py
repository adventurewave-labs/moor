"""Moor — desired-state control plane for Docker environments.

The package exposes a reconciliation control loop (observe -> diff ->
plan -> act) that keeps docker-compose stacks pinned to the state their
compose file declares. See the PRD (Moor_PRD.pdf) for the full product
specification.
"""

__version__ = "1.0.0"
