import copy
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "openclaw"
    / "normalize_config.py"
)
SPEC = importlib.util.spec_from_file_location("normalize_config", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture():
    return {
        "secrets": {
            "providers": {
                "model_key_file": {
                    "path": r"C:\Users\operator\.openclaw\secrets\model.txt",
                    "allowInsecurePath": True,
                }
            }
        },
        "agents": {
            "defaults": {
                "workspace": r"D:\workspace\weather_agent\workspace",
                "imageGenerationModel": {"primary": "comfy/workflow"},
            }
        },
        "gateway": {"bind": "loopback", "port": 18789},
        "tools": {"alsoAllow": ["message", "image_generate"]},
        "channels": {"feishu": {"groupAllowFrom": ["approved-group"]}},
        "plugins": {
            "load": {"paths": [r"D:\workspace\weather_agent\plugins"]},
            "entries": {
                "comfy": {
                    "enabled": True,
                    "config": {
                        "baseUrl": "http://127.0.0.1:8188",
                        "image": {
                            "workflowPath": r"D:\workspace\workflow.json"
                        },
                    },
                }
            },
        },
    }


class NormalizeConfigTest(unittest.TestCase):
    def test_disables_unavailable_comfy_without_leaving_windows_paths(self):
        result = MODULE.normalize_config(
            copy.deepcopy(fixture()), disable_comfy=True, comfy_base_url=None
        )

        self.assertEqual(result["gateway"]["bind"], "lan")
        self.assertFalse(result["plugins"]["entries"]["comfy"]["enabled"])
        self.assertNotIn("imageGenerationModel", result["agents"]["defaults"])
        self.assertNotIn("image_generate", result["tools"]["alsoAllow"])
        self.assertEqual(MODULE._collect_windows_paths(result), [])

    def test_configures_an_explicit_comfy_endpoint_and_workflow(self):
        result = MODULE.normalize_config(
            copy.deepcopy(fixture()),
            disable_comfy=False,
            comfy_base_url="http://comfy.internal:8188",
        )

        comfy = result["plugins"]["entries"]["comfy"]
        self.assertEqual(comfy["config"]["baseUrl"], "http://comfy.internal:8188")
        self.assertEqual(
            comfy["config"]["image"]["workflowPath"], MODULE.WORKFLOW_PATH
        )
        self.assertEqual(MODULE._collect_windows_paths(result), [])

    def test_rejects_unreviewed_group_placeholders(self):
        source = fixture()
        source["channels"]["feishu"]["groupAllowFrom"] = [
            "replace-with-reviewed-group-id"
        ]

        with self.assertRaisesRegex(ValueError, "placeholder"):
            MODULE.normalize_config(
                source, disable_comfy=True, comfy_base_url=None
            )


if __name__ == "__main__":
    unittest.main()
