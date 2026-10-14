#!/usr/bin/env bash
# Guardian 守护系统 —— 一键运行全部自检
# 用法：bash tests/run_all.sh
#
# 说明：Python 自检需要 aiohttp。若当前 python 没有 aiohttp，
# 可先创建隔离 venv：
#   python -m venv .venv && .venv/bin/pip install aiohttp
# 或直接使用任何已装 aiohttp 的解释器：PYTHON=... bash tests/run_all.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PYTHON="${PYTHON:-python3}"

echo "== 环境 =="
echo "python: $("$PYTHON" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null || echo '不可用')"
"$PYTHON" -c 'import aiohttp' 2>/dev/null && echo "aiohttp: 已安装" || { echo "aiohttp: 缺失，无法完成全部测试"; exit 1; }
export PYTHON
echo

RC=0
for t in test_core_logic.py test_probes_e2e.py test_plugin_load.py test_kernel_decoupling.py; do
  echo "== $t =="
  "$PYTHON" "$HERE/$t" || RC=1
  echo
done

echo "== test_watchdog.sh =="
bash "$HERE/test_watchdog.sh" || RC=1
echo

if [ "$RC" -eq 0 ]; then
  echo "全部自检通过。"
else
  echo "存在失败项，请查看上方输出。"
fi
exit "$RC"
