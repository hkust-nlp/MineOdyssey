"""No-model integration agent: verify live controls and reject false completion."""
import json

from harbor.agents.nop import NopAgent


class NavigationSmokeAgent(NopAgent):
    @staticmethod
    def name():
        return "navigation-smoke"

    async def run(self, instruction, environment, context):
        commands = [
            "nav start",
            "nav state",
            "nav screenshot /workspace/smoke.png",
            "nav exec 'mcapi look --yaw 5 --pitch 0'",
            "nav exec 'mcapi press MOVE_FORWARD 0.1'",
            "nav exec 'test ! -e /workspace/mcbots && echo sandbox-ok'",
            "nav claim-done",
            "nav claim-done",
            "nav claim-done",
            "nav result",
        ]
        results = []
        for command in commands:
            result = await environment.exec(command, timeout_sec=660)
            results.append({"command": command, "exit_code": result.return_code,
                            "stdout": result.stdout, "stderr": result.stderr})
            (self.logs_dir / "navigation-smoke.json").write_text(json.dumps(results, indent=2))
            if result.return_code != 0:
                raise RuntimeError(f"Smoke command failed: {command}: {result.stderr} {result.stdout}")
        completion = json.loads(results[-1]["stdout"])
        if completion.get("success") is not False or completion.get("terminal_reason") != "claim_attempts_exhausted":
            raise RuntimeError(f"False completion was not rejected: {completion}")
        await environment.download_file("/workspace/smoke.png", self.logs_dir / "smoke.png")
