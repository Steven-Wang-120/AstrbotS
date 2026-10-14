"""Guardian 守护系统 —— 探针注册表。

对应文档：B03 §5
"""

from __future__ import annotations

from typing import Any, Callable, Type

_REGISTRY: dict[str, Type[Any]] = {}


def register_probe(name: str) -> Callable[[Type[Any]], Type[Any]]:
    def deco(cls: Type[Any]) -> Type[Any]:
        cls.probe_name = name
        _REGISTRY[name] = cls
        return cls
    return deco


def get_probe(name: str) -> Type[Any] | None:
    return _REGISTRY.get(name)


def all_probe_names() -> list[str]:
    return sorted(_REGISTRY)


def build_probes(names: list[str], config, deps: dict[str, Any],
                 targets: list[str] | None = None) -> list[Any]:
    """按名字构造探针实例。

    targets 为 None 时使用探针自身声明的 target 策略（多数为按容器展开）。
    """
    out: list[Any] = []
    for name in names:
        cls = _REGISTRY.get(name)
        if cls is None:
            continue
        expand = getattr(cls, "expand_targets", None)
        if callable(expand):
            for tgt in expand(config, targets):
                out.append(cls(config, deps, tgt))
        else:
            out.append(cls(config, deps, name))
    return out
