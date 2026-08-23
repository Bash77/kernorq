"""
Default Gemini ModelClient — Phase 3.0 integration fix

Creates a ModelClient from environment configuration when no explicit
client is injected. Uses MODEL_NAME and GOOGLE_API_KEY / GEMINI_API_KEY.

If credentials are absent, raises a clear ConfigurationError that the API
translates to 503 rather than crashing.

The client respects the tool registry — it only proposes tools that are
registered, and the deterministic planner still validates everything.
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.agent.agent import validate_and_get_model_name
from app.tools.registry import ToolRegistry


class GeminiConfigurationError(RuntimeError):
    """Raised when Gemini credentials/model are not configured."""


class GeminiModelClient:
    """
    Real Gemini client implementing ModelClient Protocol.

    Uses google.genai Client with API key or Vertex AI default credentials.
    Generates a structured JSON plan for the given objective.
    """

    def __init__(self, registry: ToolRegistry, model_name: str | None = None, api_key: str | None = None) -> None:
        self.registry = registry
        self.model_name = model_name or validate_and_get_model_name()
        # Resolve API key from explicit arg or env
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY")
        # For Vertex AI, GOOGLE_CLOUD_PROJECT may be set and no API key needed
        self.project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT")

        if not self.api_key and not self.project:
            raise GeminiConfigurationError(
                "Gemini API credentials not configured: set GOOGLE_API_KEY or GEMINI_API_KEY in environment "
                "(or configure GOOGLE_CLOUD_PROJECT for Vertex AI)"
            )

        # Lazy import to avoid hard dependency at import time
        try:
            from google import genai  # type: ignore
        except ImportError as exc:
            raise GeminiConfigurationError(f"google-genai not installed: {exc}") from exc

        # Create client
        try:
            if self.api_key:
                self._client = genai.Client(api_key=self.api_key)
            else:
                # Vertex AI mode — relies on ADC
                self._client = genai.Client(vertexai=True, project=self.project, location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
            self._genai = genai
        except Exception as exc:
            raise GeminiConfigurationError(f"Failed to create Gemini client: {exc}") from exc

    def generate(self, objective: str) -> str:
        """
        Generates a structured ExecutionPlan JSON for the objective.

        The prompt constrains Gemini to only use registered tools.
        Output is sanitized downstream by the deterministic planner.
        """
        allowed_tools = self.registry.list_tools()
        prompt = f"""You are an autonomous planner for a project-delivery agent.

Objective: {objective}

You must propose a structured execution plan as JSON with:
- "objective": string (copy the objective verbatim)
- "tasks": array of tasks, each with:
  - "task_id": string (snake_case, unique, 1-64 chars)
  - "title": string (short title)
  - "description": string (detailed)
  - "tool_name": string or null (MUST be one of {allowed_tools} or null)
  - "tool_input": object (e.g., {{"directory_path": "."}} for inspect_project_workspace)
  - "dependencies": array of task_id strings (must reference existing tasks)
  - "max_attempts": integer 1-5 (default 3)

Rules:
- Only use tools in {allowed_tools}. Never invent a tool.
- Respect dependency ordering; no cycles; no self-dependency.
- Output JSON ONLY, no markdown fences, no explanation.
- Example for inspect_project_workspace:
  {{"task_id": "inspect_workspace", "title": "Inspect workspace", "description": "Inspect repository structure", "tool_name": "inspect_project_workspace", "tool_input": {{"directory_path": "."}}, "dependencies": [], "max_attempts": 2}}

Generate the plan now as JSON:
"""

        try:
            # Use generate_content with JSON mime
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=[prompt],
                config={"response_mime_type": "application/json"} if hasattr(self._client, "models") else None,
            )
            # Extract text
            text = None
            if hasattr(response, "text") and response.text:
                text = response.text
            elif hasattr(response, "candidates") and response.candidates:
                # Fallback: try candidates
                cand = response.candidates[0]
                if hasattr(cand, "content") and cand.content:
                    parts = cand.content.parts
                    if parts and hasattr(parts[0], "text"):
                        text = parts[0].text
            if not text:
                # Last resort: stringify
                text = str(response)
            # Validate it's JSON
            parsed = json.loads(text)
            # Ensure it's dict with tasks
            if not isinstance(parsed, dict):
                raise ValueError("Gemini did not return a JSON object")
            return json.dumps(parsed)
        except GeminiConfigurationError:
            raise
        except Exception as exc:
            # Wrap as InvalidPlanError downstream will handle, but we want clear message
            # Return a JSON that will be rejected by planner if Gemini fails? Better raise
            raise RuntimeError(f"Gemini generation failed: {exc}") from exc


def get_default_gemini_client(registry: ToolRegistry) -> GeminiModelClient:
    """
    Factory for API wiring — creates a GeminiModelClient from environment.

    Raises GeminiConfigurationError if credentials missing, so API can return
    503 with a clear message instead of crashing.
    """
    return GeminiModelClient(registry)


__all__ = ["GeminiModelClient", "GeminiConfigurationError", "get_default_gemini_client"]
