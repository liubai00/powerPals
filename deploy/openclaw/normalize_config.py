#!/usr/bin/env python3
"""Convert a live Windows weather-agent config into the Linux container layout."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


CONTAINER_STATE = "/home/node/.openclaw-weather-agent"
CONTAINER_WORKSPACE = f"{CONTAINER_STATE}/workspace"
PLUGIN_PATH = "/opt/weather-query-tools"
WORKFLOW_PATH = "/opt/weather-agent/config/workflows/z-image-turbo-nvfp4-api.json"
MODEL_KEY_PATH = f"{CONTAINER_STATE}/secrets/model-api-key.txt"
WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _required(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ValueError(f"missing required config key: {context}.{key}")
    return mapping[key]


def _collect_windows_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            paths.extend(_collect_windows_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(_collect_windows_paths(child))
    elif isinstance(value, str) and WINDOWS_ABSOLUTE_PATH.match(value):
        paths.append(value)
    return paths


def normalize_config(
    config: dict[str, Any], *, disable_comfy: bool, comfy_base_url: str | None
) -> dict[str, Any]:
    if disable_comfy == bool(comfy_base_url):
        raise ValueError("choose exactly one of disable_comfy or comfy_base_url")

    agents = _required(config, "agents", "root")
    defaults = _required(agents, "defaults", "agents")
    defaults["workspace"] = CONTAINER_WORKSPACE

    secrets = _required(config, "secrets", "root")
    providers = _required(secrets, "providers", "secrets")
    model_key = _required(providers, "model_key_file", "secrets.providers")
    model_key["path"] = MODEL_KEY_PATH
    model_key["allowInsecurePath"] = False

    gateway = _required(config, "gateway", "root")
    gateway["bind"] = "lan"
    gateway["port"] = 18789

    plugins = _required(config, "plugins", "root")
    load = _required(plugins, "load", "plugins")
    load["paths"] = [PLUGIN_PATH]
    entries = _required(plugins, "entries", "plugins")
    comfy = _required(entries, "comfy", "plugins.entries")
    comfy_config = _required(comfy, "config", "plugins.entries.comfy")
    image = _required(comfy_config, "image", "plugins.entries.comfy.config")
    image["workflowPath"] = WORKFLOW_PATH

    tools = _required(config, "tools", "root")
    also_allow = _required(tools, "alsoAllow", "tools")

    if disable_comfy:
        comfy["enabled"] = False
        defaults.pop("imageGenerationModel", None)
        tools["alsoAllow"] = [name for name in also_allow if name != "image_generate"]
    else:
        comfy["enabled"] = True
        comfy_config["baseUrl"] = comfy_base_url

    channels = _required(config, "channels", "root")
    feishu = _required(channels, "feishu", "channels")
    allowed_groups = _required(feishu, "groupAllowFrom", "channels.feishu")
    if not isinstance(allowed_groups, list) or not allowed_groups:
        raise ValueError("channels.feishu.groupAllowFrom must contain reviewed groups")
    if any("replace-with" in str(group_id) for group_id in allowed_groups):
        raise ValueError("channels.feishu.groupAllowFrom still contains a placeholder")

    remaining_windows_paths = _collect_windows_paths(config)
    if remaining_windows_paths:
        raise ValueError(
            "unconverted Windows path remains in config: " + remaining_windows_paths[0]
        )
    return config


def _write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)
    os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    delivery = parser.add_mutually_exclusive_group(required=True)
    delivery.add_argument("--disable-comfy", action="store_true")
    delivery.add_argument("--comfy-base-url")
    args = parser.parse_args()

    with args.input.open("r", encoding="utf-8-sig") as handle:
        source = json.load(handle)
    normalized = normalize_config(
        source,
        disable_comfy=args.disable_comfy,
        comfy_base_url=args.comfy_base_url,
    )
    _write_private_json(args.output, normalized)
    print("Normalized OpenClaw config for the Linux weather-agent container.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
