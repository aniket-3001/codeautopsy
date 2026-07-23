"""Fix Bot — closes the loop. Reads the genealogy (reasoning + crash), asks the agent to
patch its own mistake, verifies the patch with a real regression test, and opens a PR."""

from codeautopsy.fixbot.core import run_fixbot
from codeautopsy.fixbot.models import FixBotResult, FixProposal, Genealogy

__all__ = ["run_fixbot", "FixBotResult", "FixProposal", "Genealogy"]
