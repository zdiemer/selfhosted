import importlib.util
from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_runtime", ROOT / "verify-runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def render(self, *overrides):
        command = ["helm", "template", "traefik", str(ROOT), "-n", "infra",
                   "--set", "crowdsec.bouncerKey=render-only-placeholder",
                   "-s", "templates/helmchartconfig.yaml"]
        for override in overrides:
            command.extend(["--set", override])
        return yaml.safe_load(subprocess.check_output(command))

    def deployment(self, *args):
        return {"spec": {"template": {"spec": {"containers": [
            {"name": "traefik", "args": list(args)}
        ]}}}}

    def test_default_chain_and_redirect(self):
        hcc = self.render()
        values = yaml.safe_load(hcc["spec"]["valuesContent"])
        chain = values["ports"]["websecure"]["http"]["middlewares"]
        self.assertEqual(chain, ["kube-system-crowdsec-bouncer@kubernetescrd",
                                 "kube-system-edge-ratelimit@kubernetescrd"])
        runtime.verify(hcc, self.deployment(
            "--entryPoints.websecure.http.middlewares=" + ",".join(chain),
            "--entryPoints.web.http.redirections.entryPoint.to=:443",
            "--entryPoints.web.http.redirections.entryPoint.scheme=https",
        ))

    def test_missing_live_enforcement_fails(self):
        with self.assertRaises(ValueError):
            runtime.verify(self.render(), self.deployment())

    def test_crowdsec_alone_still_creates_port(self):
        hcc = self.render("rateLimit.enabled=false", "httpsRedirect.enabled=false",
                          "forwardedHeaders.trustedIPs=null")
        runtime.verify(hcc, self.deployment(
            "--entryPoints.websecure.http.middlewares=kube-system-crowdsec-bouncer@kubernetescrd"))

    def test_legacy_chart_keys_are_rejected(self):
        hcc = {"spec": {"valuesContent": "ports:\n  websecure:\n    middlewares: []\n"}}
        with self.assertRaisesRegex(ValueError, "nested under http"):
            runtime.verify(hcc, self.deployment())


if __name__ == "__main__":
    unittest.main()
