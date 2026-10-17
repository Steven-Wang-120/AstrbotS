# Guardian 独立内核镜像（B16 §9 M3 / D1-a：同栈 sidecar 容器）
#
# 构建上下文必须是**仓库根目录**（仓库根就是插件根，metadata.yaml 在根上）：
#   docker build -f deploy/kernel/Dockerfile -t guardian-kernel:local .
#
# 设计要点：
#   1. 镜像里**没有 AstrBot**——这不是省事，而是让"内核不得依赖 AstrBot"
#      （B16 不变量 I6）从"靠静态检查保证"升级为"物理上不可能"。
#   2. 内核需要 docker.sock 才能观察/操作容器（D2-b）；因此本容器**不接受
#      任何未受信输入**（只收结构化 IPC），权限边界比把 socket 挂在 astrbot
#      容器上更紧（B16 §11-3）。
#   3. 不用 privileged，只用 group_add 收敛 socket 组权限。
#
# 目录约定：镜像里插件被放到 /app/src/astrbot_plugin_guardian/，因此
# PYTHONPATH=/app/src，`python -m astrbot_plugin_guardian.kernel` 才解析得到。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# 先装依赖（利用层缓存：源码改动不会触发重新 pip install）
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 再拷插件本体。仓库根即插件根，但仓库里还有 tests/ deploy/ docs/ 等
# 非插件目录，用 .dockerignore 在构建上下文阶段剔除（见仓库根 .dockerignore）。
COPY . /app/src/astrbot_plugin_guardian

# 数据/共享卷挂载点（compose 会把宿主的 ./data 挂到 /AstrBot/data）
RUN mkdir -p /AstrBot/data/guardian

# 部署期冒烟开关：docker run --rm guardian-kernel:local --selftest
ENTRYPOINT ["python", "-m", "astrbot_plugin_guardian.kernel"]
