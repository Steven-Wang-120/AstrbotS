"""打包 astrbot_plugin_guardian 为可直接上传 WebUI 的 zip。

约束（来自 AstrBot 插件手册与 updater 实现）：
- 压缩包根目录必须是 `astrbot_plugin_guardian/`，其下直接有 metadata.yaml；
- 排除 __pycache__ / *.pyc / .DS_Store / 日志等非必需文件；
- 体积远小于市场 16MB 限制。

仓库布局说明：仓库根**就是**插件根（metadata.yaml / main.py / core/ 等都在根目录），
因此打包源是仓库根，但必须显式排除仓库里**不属于插件本体**的目录与文件
（tests / deploy / docs / 构建脚本 / 旧 README 等），否则它们会被一并塞进插件包。
"""
from __future__ import annotations

import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = "astrbot_plugin_guardian"
SRC = ROOT  # 仓库根即插件根


def _read_version() -> str:
    """从 `metadata.yaml` 读版本号，避免与元数据脱节。

    早先这里硬编码过 `-0.6.0.zip`，版本一升就打不出新包名，或者更糟——
    打出来的包名和里面的 metadata 不一致。以 metadata.yaml 为唯一真相源。
    """
    path = os.path.join(ROOT, "metadata.yaml")
    try:
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                key, sep, value = line.partition(":")
                if sep and key.strip() == "version":
                    return value.strip().strip("\"'")
    except OSError:
        pass
    print("警告：未能从 metadata.yaml 读到 version，产物名退回 0.0.0")
    return "0.0.0"


VERSION = _read_version()
OUT = os.path.join(ROOT, f"{PKG}-{VERSION}.zip")

#: 仓库根上**不属于插件本体**的顶层目录（测试、部署、文档、VCS、缓存）。
EXCLUDE_DIRS = {
    "__pycache__", "tests", "deploy", "docs", "src", ".git", ".github",
    ".venv", "venv", ".ruff_cache", ".pytest_cache", ".mypy_cache",
    ".idea", ".vscode", ".workbuddy-ai",
}
#: 仓库根上不属于插件本体的顶层文件（构建脚本、仓库级配置、内部草稿）。
#: 注意 logo.png / README.md / LICENSE **要保留**——它们是插件包的标准组成
#: （市场展示图标、用户说明、许可证）。
EXCLUDE_TOP_FILES = {
    ".gitignore", ".gitattributes", ".dockerignore", "pyproject.toml",
    "build_plugin_zip.py", "README-old.tmp", "AstrBotS-Guardian-Handoff.docx",
}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".zip", ".docx")


def main() -> int:
    if not os.path.isfile(os.path.join(SRC, "metadata.yaml")):
        print(f"ERROR: 未找到 {SRC}/metadata.yaml")
        return 1

    if os.path.exists(OUT):
        os.remove(OUT)

    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for dirpath, dirnames, filenames in os.walk(SRC):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            at_root = os.path.abspath(dirpath) == os.path.abspath(SRC)
            for name in sorted(filenames):
                if name in EXCLUDE_FILES or name.endswith(EXCLUDE_SUFFIX):
                    continue
                if at_root and name in EXCLUDE_TOP_FILES:
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, SRC)
                zf.write(full, f"{PKG}/{rel.replace(os.sep, '/')}")
                count += 1

    size = os.path.getsize(OUT)
    with zipfile.ZipFile(OUT) as zf:
        names = zf.namelist()
        bad = [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
        top = {n.split("/", 1)[0] for n in names}
        has_meta = f"{PKG}/metadata.yaml" in names

    leaked = sorted({n.split("/", 1)[1].split("/", 1)[0] for n in names
                     if n.split("/", 1)[1].split("/", 1)[0] in EXCLUDE_DIRS})
    if leaked:
        print(f"ERROR: 非插件目录被误打包: {leaked}")
        return 1

    print(f"输出: {OUT}")
    print(f"文件数: {count}  体积: {size / 1024:.1f} KB")
    print(f"顶层目录: {sorted(top)}")
    print(f"根目录 metadata.yaml: {'有' if has_meta else '缺失'}")
    print(f"垃圾文件: {bad if bad else '无'}")
    return 0 if (has_meta and not bad and top == {PKG}) else 1


if __name__ == "__main__":
    sys.exit(main())
