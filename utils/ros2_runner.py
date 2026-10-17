"""Guardian 守护系统 —— ROS2 命令执行器（白名单化）。

对应文档：B07 §3

两种后端：
- local：直接在本机执行 ros2 命令（要求本机已 source ROS 环境）
- docker_exec：通过 Docker API 在指定容器内执行

安全：只允许 ros2 / systemctl / docker 前缀；恢复命令必须来自配置白名单映射。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from ..core.log import logger
from ..core.safety import ALLOWED_COMMAND_PREFIXES, validate_command
from .dockerclient import DockerClient, DockerError, DockerUnavailable
from .shell import CmdResult, run_argv


@dataclass
class Ros2Output:
    rc: int
    text: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    def lines(self) -> list[str]:
        return [ln.strip() for ln in (self.text or "").splitlines() if ln.strip()]


class Ros2Runner:
    def __init__(self, config, docker: DockerClient) -> None:
        self.config = config
        self.docker = docker

    # ------------------------------------------------------------------ #
    @property
    def backend(self) -> str:
        return self.config.ros2.exec_backend or "local"

    def _env(self) -> dict[str, str] | None:
        env = self.config.ros2.env or {}
        if not env:
            return None
        import os
        merged = dict(os.environ)
        merged.update({str(k): str(v) for k, v in env.items()})
        return merged

    async def _run_raw(self, argv: list[str], timeout: float | None = None) -> Ros2Output:
        timeout = timeout or float(self.config.ros2.command_timeout or 15)
        if self.backend == "docker_exec":
            container = self.config.ros2.docker_container
            if not container:
                return Ros2Output(rc=-1, text="ros2.docker_container 未配置")
            try:
                rc, text = await self.docker.exec_run(container, argv, timeout=timeout)
                return Ros2Output(rc=rc, text=text)
            except DockerUnavailable as exc:
                return Ros2Output(rc=-2, text=f"Docker 不可达: {exc}")
            except DockerError as exc:
                return Ros2Output(rc=-3, text=f"exec 失败: {exc}")
        result: CmdResult = await run_argv(argv, timeout=timeout, env=self._env())
        return Ros2Output(rc=result.rc, text=result.text(), timed_out=result.timed_out)

    # ------------------------------------------------------------------ #
    async def node_list(self) -> Ros2Output:
        return await self._run_raw(["ros2", "node", "list"])

    async def topic_list(self) -> Ros2Output:
        return await self._run_raw(["ros2", "topic", "list"])

    async def topic_info(self, topic: str) -> Ros2Output:
        return await self._run_raw(["ros2", "topic", "info", topic])

    async def topic_hz(self, topic: str, window: int = 5) -> Ros2Output:
        return await self._run_raw(
            ["ros2", "topic", "hz", "--window", str(window), topic],
            timeout=float(self.config.ros2.command_timeout or 15) + 5)

    async def doctor_report(self) -> Ros2Output:
        return await self._run_raw(["ros2", "doctor", "--report"])

    # ------------------------------------------------------------------ #
    async def restart_node(self, node: str) -> Ros2Output:
        """执行配置中为该节点定义的恢复命令（白名单）。"""
        mapping = self.config.ros2.node_restart_commands or {}
        cmd = mapping.get(node)
        if not cmd:
            return Ros2Output(rc=-1, text=f"节点 {node} 未配置恢复命令（白名单为空）")
        ok, msg = validate_command(cmd)
        if not ok:
            logger.warning(f"[guardian] 拒绝执行非法恢复命令: {msg}")
            return Ros2Output(rc=-1, text=f"恢复命令被拒绝: {msg}")
        argv = shlex.split(cmd)
        if argv and argv[0] not in ALLOWED_COMMAND_PREFIXES:
            return Ros2Output(rc=-1, text=f"命令前缀不在白名单: {argv[0]}")
        return await self._run_raw(argv)


def parse_topic_hz(text: str) -> float:
    """从 `ros2 topic hz` 输出解析平均频率；失败返回 -1。"""
    for line in (text or "").splitlines():
        low = line.lower()
        if "average rate" in low:
            try:
                return float(low.split(":", 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                continue
    return -1.0


def parse_node_list(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("/")]


def parse_topic_list(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("/")]
