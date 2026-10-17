"""Guardian 守护系统 —— AstrBot WebUI 登录状态页（B17 §10）。

**存在的理由**：QQ 掉线时，插件唯一的 IM 通道同时失效，"推二维码给管理员"
是自相矛盾的（缺陷 P3）。所以失败信息必须有一条**不依赖 QQ** 的出口——就是
这个页面。它走 dashboard 鉴权，和 QQ 是否在线完全无关。

域边界（B17 §10.4）：本模块属于域 B，允许 `import astrbot`，但**不得**触碰
docker / 动作 / 安全判定——所有动作经 `runtime.request_qq_login()` 转发内核。

设计取舍（L3）：二维码**不做**服务端渲染。`GetQQLoginQrcode` 返回的是二维码
的**载荷 URL**（不是图片），NapCat 前端是客户端渲染的。所以这里只给"打开
NapCat 扫码页"的按钮，渲染交给 NapCat 自己。
"""

from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any

PLUGIN_NAME = "astrbot_plugin_guardian"
ROUTE_LOGIN = f"{PLUGIN_NAME}/login"
ROUTE_RETRY = f"{PLUGIN_NAME}/login/retry"


# --------------------------------------------------------------------------- #
# 渲染（纯函数，可离线单测）
# --------------------------------------------------------------------------- #
def _fmt_ts(value: Any) -> str:
    try:
        ts = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _yn(value: Any, yes: str = "是", no: str = "否") -> str:
    if value is None:
        return "未知"
    return yes if value else no


def render_html(status: dict[str, Any], *, retry_path: str = ROUTE_RETRY) -> str:
    """把内核给的登录状态渲染成一页可读的 HTML。"""
    status = status or {}
    last = status.get("last_failure") or {}
    if not isinstance(last, dict):
        last = {}
    online = status.get("isLogin")
    badge = "在线" if online else ("离线" if online is False else "未知")
    color = {"在线": "#1a7f37", "离线": "#b42318"}.get(badge, "#8a6d3b")

    hold_until = float(status.get("auth_hold_until") or 0)
    now = float(status.get("now") or time.time())
    hold_left = max(0, int(hold_until - now)) if hold_until else 0
    hold_text = f"密码失败静默中，剩余 {hold_left}s" if hold_left else "无"

    base = str(status.get("webui_public_base") or "").rstrip("/")
    token = str(status.get("webui_token_for_link") or "")
    if base:
        link = f"{base}/webui?token={token}" if token else f"{base}/webui"
        direct = f'<a href="{_esc(link)}" target="_blank" rel="noreferrer">打开 NapCat WebUI（已带 token）</a>'
    else:
        direct = "未配置 qq.webui_public_base，无法生成浏览器侧链接"

    rows = [
        ("登录态", badge),
        ("isLogin / isOffline", f"{_esc(status.get('isLogin'))} / {_esc(status.get('isOffline'))}"),
        ("协议端错误", _esc(status.get("loginError") or "-")),
        ("最近失败分类", _esc(last.get("kind") or "-")),
        ("最近失败原因", _esc(last.get("message") or "-")),
        ("最近失败时间", _fmt_ts(last.get("ts"))),
        ("人工接管链接", _esc(last.get("url") or "-")),
        ("最近成功时间", _fmt_ts(status.get("last_success_at"))),
        ("静默期", hold_text),
        ("QQ 号", _esc(status.get("account") or "-")),
        ("重登策略", _esc(status.get("relogin_method") or "-")),
        ("已配凭据", _yn(status.get("has_credential"))),
        ("WebUI 可达", _yn(status.get("available"))),
    ]
    body = "\n".join(
        f"      <tr><th>{_esc(k)}</th><td>{v}</td></tr>" for k, v in rows)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Guardian · QQ 登录状态</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 24px; background: #f6f7f9; color: #1f2328; }}
  .card {{ max-width: 860px; margin: 0 auto; background: #fff; border-radius: 10px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); padding: 24px 28px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px;
            background: {color}; color: #fff; font-size: 13px; margin-left: 8px; }}
  .sub {{ color: #656d76; font-size: 13px; margin-bottom: 18px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eaeef2;
            vertical-align: top; word-break: break-all; }}
  th {{ width: 200px; color: #57606a; font-weight: 500; }}
  .actions {{ margin-top: 20px; display: flex; gap: 12px; align-items: center;
              flex-wrap: wrap; }}
  button {{ background: #1f6feb; color: #fff; border: 0; border-radius: 6px;
            padding: 8px 16px; font-size: 14px; cursor: pointer; }}
  button:hover {{ background: #1a5fd0; }}
  a {{ color: #1f6feb; }}
  .note {{ margin-top: 18px; font-size: 12px; color: #656d76; line-height: 1.7; }}
</style>
</head>
<body>
  <div class="card">
    <h1>QQ 登录状态<span class="badge">{_esc(badge)}</span></h1>
    <div class="sub">本页面不依赖 QQ 通道：QQ 离线时仍可从这里看到原因并人工接管。</div>
    <table>
{body}
    </table>
    <div class="actions">
      <form method="post" action="{_esc(retry_path)}">
        <button type="submit">重试登录恢复</button>
      </form>
      <span>{direct}</span>
    </div>
    <div class="note">
      重试会同步执行一次完整阶梯（有条件重启 → 等待就绪 → 账密登录 → 复核写回），
      最长约 4 分钟，请勿重复点击。若 QQ 需要验证码或新设备验证，请用上面的
      NapCat WebUI 链接人工接管。
    </div>
  </div>
</body>
</html>
"""


def _html_response(text: str):
    from starlette.responses import HTMLResponse  # 域 B：dashboard 依赖 starlette
    return HTMLResponse(text, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def register(context, runtime) -> bool:
    """注册登录状态页与重试接口。返回是否注册成功（失败由调用方降级）。

    AstrBot 的插件 Web API 走 dashboard 鉴权，浏览器带 `astrbot_dashboard_jwt`
    Cookie 即可直接打开；`view_handler(**path_values)` 的返回值若本身是
    `Response` 会被原样返回，所以这里可以直接吐 HTML。
    """
    register_web_api = getattr(context, "register_web_api", None)
    if not callable(register_web_api):
        return False

    async def login_view(**_kwargs):
        status = await runtime.qq_login_status()
        status = dict(status or {})
        # 页面在 dashboard 鉴权之后，允许带 token 生成直达链接（B17 §10.3）。
        try:
            status["webui_token_for_link"] = str(runtime._cfg_view.qq.webui_token or "")
        except Exception:  # noqa: BLE001 —— 取不到就不带 token，不阻断页面
            status["webui_token_for_link"] = ""
        return _html_response(render_html(status))

    async def retry_view(**_kwargs):
        ok, detail = await runtime.request_qq_login("WebUI 登录状态页手动重试")
        status = await runtime.qq_login_status()
        status = dict(status or {})
        try:
            status["webui_token_for_link"] = str(runtime._cfg_view.qq.webui_token or "")
        except Exception:  # noqa: BLE001
            status["webui_token_for_link"] = ""
        banner = ("<p style='color:#1a7f37'>重试结果：成功 —— " if ok
                  else "<p style='color:#b42318'>重试结果：未成功 —— ")
        page = render_html(status)
        injected = page.replace(
            '<div class="actions">',
            f"{banner}{_esc(detail)}</p>\n    <div class=\"actions\">", 1)
        return _html_response(injected)

    register_web_api(ROUTE_LOGIN, login_view, ["GET"], "Guardian 登录状态与人工接管")
    register_web_api(ROUTE_RETRY, retry_view, ["POST"], "触发一次 QQ 登录恢复")
    return True


def write_fallback(runtime) -> str:
    """`register_web_api` 不可用时的降级路径：把同一份 HTML 落到磁盘。

    返回写入的绝对路径（供日志与审计打印）；失败返回空串。
    """
    try:
        data_dir = Path(str(getattr(runtime, "data_dir", "") or ""))
        if not str(data_dir):
            return ""
        path = data_dir / "guardian" / "login.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_html({"loginError": "register_web_api 不可用，静态降级页"}),
                        encoding="utf-8")
        return str(path.resolve())
    except OSError:
        return ""
