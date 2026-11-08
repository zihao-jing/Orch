"""Orch — a general-purpose orchestrator for teams of CLI coding agents.

A config file declares one or more *monitors*. Each monitor owns a workspace,
a goal, a controller, and a roster of agents (workers, verifiers, or any custom
role). The controller launches the agents in the workspace, keeps their
sessions alive across turns, routes tasks and feedback between them, and every
round / turn is recorded to an output directory for human review.
"""

__version__ = "0.1.0"
