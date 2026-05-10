"""VRChat Friend Radar LLM tool exports.

Expose a single builder that, given the plugin instance, returns a list of
ready-to-register FunctionTool instances for ``context.add_llm_tools``.
"""

from .vrc_tools import build_llm_tools

__all__ = ['build_llm_tools']
