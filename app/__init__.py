from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from google.adk.apps import App
from app.agent import AGENT_INSTRUCTION, MODEL_NAME, root_agent

app = App(
    name="app",
    root_agent=root_agent,
)

__all__ = ["root_agent", "app", "MODEL_NAME", "AGENT_INSTRUCTION"]
