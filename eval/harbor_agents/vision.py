"""Compatibility entry point; all decisions now use the original agent.main."""
from eval.harbor_agents.original import OriginalNavigationAgent

NavigationVisionAgent = OriginalNavigationAgent
