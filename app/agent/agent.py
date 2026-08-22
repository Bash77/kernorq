
from __future__ import annotations

import os
import re
from google.adk.agents import Agent
from app.tools import inspect_project_workspace

MINIMUM_MODEL_VERSION = (3, 5)


def validate_and_get_model_name() -> str:
    """Validates that MODEL_NAME is configured and satisfies the Gemini 3.5+ requirement.

    Raises:
        ValueError: If MODEL_NAME is missing, empty, or below Gemini 3.5.
    """
    model_name = os.getenv("MODEL_NAME", "gemini-3.5-flash").strip()
    if not model_name:
        raise ValueError(
            "MODEL_NAME environment variable is required and must not be empty."
        )

    # Match gemini-X.Y patterns to enforce hackathon >= 3.5 minimum
    match = re.match(r"^gemini-(\d+)(?:\.(\d+))?", model_name.lower())
    if match:
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) is not None else 0
        if (major, minor) < MINIMUM_MODEL_VERSION:
            raise ValueError(
                f"MODEL_NAME '{model_name}' is not compliant with the All Things Agentic Hackathon "
                f"requirement. Minimum required model is Gemini 3.5 or newer (found version {major}.{minor})."
            )

    return model_name


MODEL_NAME = validate_and_get_model_name()

AGENT_INSTRUCTION = """You are an autonomous project-execution agent.

Your job is to understand a project-delivery objective and inspect the workspace when necessary.

You MUST NOT claim to have completed an action unless a real tool has performed and verified it.

You MUST clearly distinguish:
- plan
- observation
- tool result
- verified result

At this phase, you are allowed only to inspect the workspace using the available inspection tool.
You must not pretend that execution, recovery, persistence, or completion already exist."""

root_agent = Agent(
    name="root_agent",
    model=MODEL_NAME,
    instruction=AGENT_INSTRUCTION,
    description="Autonomous execution agent capable of inspecting project workspace.",
    tools=[inspect_project_workspace],
)

__all__ = ["root_agent", "MODEL_NAME", "AGENT_INSTRUCTION", "validate_and_get_model_name", "MINIMUM_MODEL_VERSION"]

