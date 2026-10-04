"""The historical entry point must no longer run a second model loop."""
import importlib.util
import unittest

@unittest.skipUnless(importlib.util.find_spec('harbor'), 'Requires Harbor host environment')
class OriginalEntrypointTests(unittest.TestCase):
    def test_legacy_entrypoint_delegates_to_original(self):
        from eval.harbor_agents.vision import NavigationVisionAgent
        from eval.harbor_agents.original import OriginalNavigationAgent
        self.assertIs(NavigationVisionAgent, OriginalNavigationAgent)
