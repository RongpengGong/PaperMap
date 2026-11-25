"""Agent squads (智能体小队): a lead plans a task board, members work through it in parallel with
read-only library tools, and everything that would change the library comes back as a proposal."""
from .runtime import SquadRunner, estimate, follow_up, squad_runner, stop  # noqa: F401
