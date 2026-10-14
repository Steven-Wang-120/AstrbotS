"""Read-only validation of WORKSPACE sources inside the real AstrBot container.

Loads plugin source into an in-memory importer, not the deployed plugin directory.
No reload, credentials, QQ messages, writes to container files, or Docker actions.
Run on host: python tests/live/verify_workspace.py [astrbot-local]
"""
import base64
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "src" / "astrbot_plugin_guardian"

BOOTSTRAP = r'''
import asyncio, base64, importlib, importlib.abc, importlib.util, json, sys
payload = json.loads(base64.b64decode(sys.stdin.read()))
class MemoryImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        record = payload["modules"].get(fullname)
        if record is None:
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=record["package"])
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        record = payload["modules"][module.__name__]
        module.__file__ = "/__guardian_memory__/" + record["relative"]
        if record["package"]:
            module.__path__ = []
        exec(compile(record["source"], module.__file__, "exec"), module.__dict__)
sys.meta_path.insert(0, MemoryImporter())
for name in sorted(payload["modules"]):
    importlib.import_module(name)
print("IMPORTED", len(payload["modules"]), "workspace modules using REAL AstrBot API")
from astrbot.core.config.astrbot_config import AstrBotConfig
config_defaults = AstrBotConfig._config_schema_to_default_config(dict.__new__(AstrBotConfig), payload["schema"])
from astrbot_plugin_guardian.core.config import DEFAULTS, build_config
def differences(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        return [item for key in sorted(set(a) | set(b)) for item in differences(a.get(key), b.get(key), path + "." + key)]
    return [] if a == b else [(path, a, b)]
mismatches = differences(config_defaults, DEFAULTS)
assert not mismatches, "Schema defaults differ: " + repr(mismatches)
print("SCHEMA real AstrBot conversion passed; defaults match")
from astrbot_plugin_guardian.utils.dockerclient import DockerClient
from astrbot_plugin_guardian.probes.container_memory import ContainerMemoryProbe, memory_working_set
from astrbot_plugin_guardian.probes.uptime import UptimeProbe
class MemoryState:
    def __init__(self): self.data = {}
    def get(self, key, default=None): return self.data.get(key, default)
    def set(self, key, value): self.data[key] = value
async def verify():
    config = build_config({"dry_run": True, "containers": {
        "astrbot": "astrbot-local", "napcat": "napcat-local"}, "lifecycle": {
        "uptime_targets": ["astrbot-local", "napcat-local"]}})
    docker = DockerClient()
    state = MemoryState()
    try:
        await docker.negotiate()
        report = []
        for name in config.all_containers:
            raw = await docker.stats(name)
            effective, cache, source = memory_working_set(raw["memory_stats"])
            stats = raw["memory_stats"].get("stats", {})
            if "total_inactive_file" in stats:
                inactive = stats["total_inactive_file"]
            elif "inactive_file" in stats:
                inactive = stats["inactive_file"]
            else:
                inactive = stats.get("cache", 0)
            usage = raw["memory_stats"]["usage"]
            expected = usage - inactive if 0 <= inactive < usage else usage
            assert effective == expected
            probe = ContainerMemoryProbe(config, {"docker": docker, "state": state}, name)
            first = await probe.check()
            second = await probe.check()
            assert not second.metrics["new_sample"]
            assert not first.metrics["leak"] and not second.metrics["leak"]
            report.append({"name": name, "working_set_mib": round(effective / 1048576, 2),
                           "raw_mib": round(usage / 1048576, 2), "cache_mib": round(cache / 1048576, 2),
                           "source": source, "status": second.status.value,
                           "duplicate_patrol_new_sample": second.metrics["new_sample"]})
        uptime = await UptimeProbe(config, {"docker": docker, "state": state}, "system").check()
        assert "uptime_hours" in uptime.metrics
        print("LIVE_READONLY", json.dumps({"memory": report, "uptime": uptime.metrics}, ensure_ascii=False))
    finally:
        await docker.close()
asyncio.run(verify())
print("PASS: no production reload or destructive actions performed")
'''


def main():
    modules = {}
    for path in sorted(PLUGIN.rglob("*.py")):
        relative = path.relative_to(PLUGIN)
        package = path.name == "__init__.py"
        parts = list(relative.with_suffix("").parts)
        if package:
            parts.pop()
        name = ".".join(["astrbot_plugin_guardian"] + parts)
        modules[name] = {"package": package, "relative": str(relative).replace("\\", "/"),
                         "source": path.read_text(encoding="utf-8")}
    payload = {"modules": modules,
               "schema": json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))}
    result = subprocess.run(["docker", "exec", "-i", sys.argv[1] if len(sys.argv) > 1 else "astrbot-local",
                             "/usr/local/bin/python", "-B", "-c", BOOTSTRAP],
                            input=base64.b64encode(json.dumps(payload).encode()).decode(),
                            text=True, capture_output=True, shell=False, timeout=120)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
