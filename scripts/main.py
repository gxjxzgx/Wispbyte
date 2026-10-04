#!/usr/bin/env python3
"""
Wispbyte 自动开机/重启脚本（优化版）

主要改动：
  - 所有 API 请求改为在浏览器内 fetch（指纹/Cookie 一致，避免 requests 被 CF 返回 520）
  - 每次操作前 ensure_connected，解决 UC 模式 chromedriver 断连（Errno 111）
  - Cancel/Alert 之后不再强制刷新页面；没出现验证弹窗则再点一次 Start
  - 验证通过后显式调用 Start API
  - 修复 login 重试、关闭按钮误点、续期判断恒真、强制重启逻辑等
  - 临时目录清理、单账号异常隔离、失败时退出码非 0
"""

import os
import sys
import time
import json
import shutil
import logging
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional

import requests
from seleniumbase import SB

# ====================== 配置 ======================
LOGIN_URL = "https://wispbyte.com/client"
CONSOLE_URL_TEMPLATE = "https://wispbyte.com/client/servers/{identifier}/console"

API_STATUS_PATH = "/client/api/servers/status"
API_CAPTCHA_STATUS_PATH = "/client/api/server/start-captcha/status"
API_CAPTCHA_REWARDED_PATH = "/client/api/server/start-captcha/rewarded"
API_SERVER_START_PATH = "/client/api/server/start"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", str(Path.cwd()))
OUTPUT_DIR = Path(WORKSPACE) / "output/screenshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


START_BTN_JS = "var b=document.querySelector('#start-btn,#restart-btn');if(b){b.click();return true;}return false;"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("wispbyte_restart")
for _noisy in ("seleniumbase", "selenium", "urllib3", "undetected_chromedriver"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


# ====================== 工具函数 ======================
def mask_email(email: str) -> str:
    if "@" not in email:
        if len(email) <= 2:
            return (email[0] + "***") if email else "***"
        return email[0] + "***" + email[-1]
    local, domain = email.split("@", 1)
    if not local:
        m = "***"
    elif len(local) == 1:
        m = local + "***"
    else:
        m = local[0] + "***" + local[-1]
    return f"{m}@{domain}"


def mask_server_id(identifier: str) -> str:
    if not identifier or len(identifier) <= 4:
        return "***"
    return identifier[:2] + "***" + identifier[-2:]


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}天{h}小时{m}分"
    if h:
        return f"{h}小时{m}分"
    return f"{m}分{sec}秒" if m else f"{sec}秒"


def log(msg: str, level: str = "INFO"):
    lv = {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}.get(level, logging.INFO)
    logger.log(lv, f"[{level}] {msg}")


def send_tg_photo(token: str, chat_id: str, photo_path: str, caption: str):
    if not token or not chat_id:
        return
    if not photo_path or not os.path.exists(photo_path):
        log(f"截图文件不存在: {photo_path}", "WARN")
        return
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=30,
            )
        resp.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 通知异常: {e}", "ERROR")


def restart_warp() -> bool:
    log("正在重启 WARP 以更换 IP...")
    try:
        old_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"当前 IP: {old_ip}")
    except Exception:
        pass
    try:
        run = lambda *a: subprocess.run(["sudo", "warp-cli", "--accept-tos", *a],
                                        timeout=30, capture_output=True)
        run("disconnect")
        time.sleep(3)
        r = run("registration", "delete")
        if r.returncode != 0:
            log("删除注册失败（可能未注册），继续...", "WARN")
        r = run("registration", "new")
        if r.returncode != 0:
            raise RuntimeError("registration new 失败")
        time.sleep(3)
        r = run("connect")
        if r.returncode != 0:
            raise RuntimeError("connect 失败")
        time.sleep(10)
        new_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"WARP 重连成功，新 IP: {new_ip}")
        return True
    except Exception as e:
        log(f"WARP 重连失败: {e}", "ERROR")
        return False


def ensure_connected(sb):
    """UC 模式下 chromedriver 可能被主动断开，操作前先确认/重连。"""
    try:
        _ = sb.driver.title
        return
    except Exception:
        pass
    for fn, args in (("connect", ()), ("reconnect", (2,))):
        try:
            getattr(sb.driver, fn)(*args)
            _ = sb.driver.title
            return
        except Exception:
            continue


def safe_url(sb) -> str:
    ensure_connected(sb)
    try:
        return sb.get_current_url() or ""
    except Exception:
        return ""


def take_screenshot(sb, account_index: int, suffix: str) -> str:
    filename = f"acc{account_index}-{suffix}-{datetime.now().strftime('%H%M%S')}.png"
    filepath = str(OUTPUT_DIR / filename)
    try:
        ensure_connected(sb)
        sb.save_screenshot(filepath)
        log(f"📸 截图保存: {filepath}")
        return filepath
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return ""


def block_ads_modals(sb):
    css = ".wisp-offer-modal, .instagram-modal, .qc-cmp2-summary-section { display: none !important; }"
    try:
        ensure_connected(sb)
        sb.execute_script(
            "var s=document.createElement('style');s.textContent=%s;document.head.appendChild(s);"
            % json.dumps(css)
        )
        log("✅ 已注入广告屏蔽 CSS")
    except Exception as e:
        log(f"注入屏蔽 CSS 失败: {e}", "WARN")


# ====================== 浏览器内 fetch（核心）======================
def browser_fetch(sb, method: str, path: str, body=None, timeout: int = 30) -> Tuple[int, dict, str]:
    """在页面上下文里发请求，指纹与 Cookie 与浏览器完全一致。返回 (status, json, text预览)。"""
    ensure_connected(sb)
    body_js = json.dumps(json.dumps(body)) if body is not None else "null"
    js = f"""
    var cb = arguments[arguments.length - 1];
    var opt = {{method: {json.dumps(method)}, credentials: 'include',
               headers: {{'Accept': 'application/json',
                          'X-Requested-With': 'XMLHttpRequest',
                          'Content-Type': 'application/json'}}}};
    var b = {body_js};
    if (b) opt.body = b;
    fetch({json.dumps(path)}, opt)
      .then(function(r) {{ return r.text().then(function(t) {{ cb({{status: r.status, text: t}}); }}); }})
      .catch(function(e) {{ cb({{status: 0, text: String(e)}}); }});
    """
    try:
        sb.driver.set_script_timeout(timeout)
        res = sb.driver.execute_async_script(js) or {}
    except Exception as e:
        return 0, {}, str(e)[:200]
    text = res.get("text", "") or ""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    preview = "(HTML 页面)" if text.lstrip()[:15].lower().startswith(("<!doctype", "<html")) else text[:200]
    return int(res.get("status", 0) or 0), data, preview


def api_get_captcha_status(sb) -> dict:
    st, data, prev = browser_fetch(sb, "GET", API_CAPTCHA_STATUS_PATH)
    if st != 200:
        log(f"captcha status HTTP {st}: {prev}", "WARN")
        return {}
    return data


def api_refresh_rewarded(sb) -> bool:
    st, data, prev = browser_fetch(sb, "POST", API_CAPTCHA_REWARDED_PATH, {})
    log(f"rewarded: {st} {prev[:80]}")
    return st == 200 and data.get("success") is not False


def api_start_server(sb, identifier: str) -> Tuple[bool, str]:
    st, data, prev = browser_fetch(sb, "POST", API_SERVER_START_PATH, {"serverId": identifier})
    log(f"Start API: {st} {prev[:120]}")
    ok = st == 200 and data.get("success") is not False
    return ok, f"{st} {prev}"


def ensure_start_gate(sb) -> bool:
    """确保具备启动资格：status 有效 → 直接通过；否则尝试 rewarded 续期并复查。"""
    status = api_get_captcha_status(sb)
    if status.get("valid"):
        log(f"✅ 启动资格有效（到期: {status.get('expiresAt') or status.get('expires') or '?'}），跳过广告")
        return True
    log("启动资格无效或未知，尝试 rewarded 续期...")
    if api_refresh_rewarded(sb) and api_get_captcha_status(sb).get("valid"):
        log("✅ rewarded 续期成功")
        return True
    log("rewarded 未能获得资格，需浏览器验证流程", "WARN")
    return False


def get_servers(sb) -> List[str]:
    log("请求服务器列表...")
    st, data, prev = browser_fetch(sb, "GET", API_STATUS_PATH)
    ids = [str(s.get("identifier")) for s in (data.get("servers") or []) if s.get("identifier")]
    if ids:
        log(f"成功获取服务器列表，共 {len(ids)} 台: {[mask_server_id(i) for i in ids]}")
        return ids
    log(f"API 未返回服务器 ({st} {prev[:60]})，尝试 DOM 提取", "WARN")
    try:
        dom_ids = sb.execute_script("""
            return Array.from(document.querySelectorAll('[data-server-id], .server-card, .server-item'))
              .map(function(el){return el.getAttribute('data-server-id') || el.id;}).filter(Boolean);
        """)
        if dom_ids:
            log(f"从 DOM 提取到 {len(dom_ids)} 台服务器")
            return list(dom_ids)
    except Exception as e:
        log(f"DOM 提取失败: {e}", "WARN")
    log("未能获取任何服务器标识符", "ERROR")
    return []


def get_server_info(sb, identifier: str) -> dict:
    st, data, _ = browser_fetch(sb, "GET", API_STATUS_PATH)
    for srv in data.get("servers") or []:
        if srv.get("identifier") == identifier:
            return srv
    return {}


def get_server_status(sb, identifier: str) -> Optional[str]:
    val = get_server_info(sb, identifier).get("current_state")
    return str(val).strip() if val else None


def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    else:
        yield prefix, obj


def extract_uptime_seconds(info: dict) -> Optional[float]:
    """
    从状态接口返回里提取服务器已运行秒数。
    支持：*uptime*（数值，key 含 ms 或数值过大时按毫秒）、started_at 类时间戳/ISO 字符串。
    """
    now = time.time()
    for path, val in _flatten(info):
        key = path.lower()
        leaf = key.split(".")[-1]
        if "uptime" in leaf and isinstance(val, (int, float)) and not isinstance(val, bool):
            if val <= 0:
                return None
            return val / 1000.0 if ("ms" in leaf or val > 3e8) else float(val)
    for path, val in _flatten(info):
        leaf = path.lower().split(".")[-1]
        if leaf in ("started_at", "startedat", "start_time", "starttime", "last_started", "laststarted"):
            try:
                if isinstance(val, (int, float)):
                    ts = val / 1000.0 if val > 1e11 else float(val)
                else:
                    ts = datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
                if 0 < ts <= now:
                    return now - ts
            except Exception:
                continue
    return None


def is_server_running(status: Optional[str]) -> bool:
    return bool(status) and "running" in status.lower()


def status_to_chinese(status: Optional[str]) -> str:
    if not status:
        return "未知"
    s = status.lower().strip()
    mapping = {"running": "运行中", "offline": "离线", "stopped": "已停止", "starting": "启动中",
               "stopping": "停止中", "installing": "安装中", "suspended": "已暂停", "unknown": "未知"}
    for k, cn in mapping.items():
        if k in s:
            return cn
    return status


# ====================== Turnstile ======================
def check_turnstile_solved(sb) -> bool:
    try:
        ensure_connected(sb)
        return bool(sb.execute_script("""
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 20) return true;
            var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe && iframe.getAttribute("data-state") === "solved") return true;
            var success = document.getElementById('success');
            return !!(success && getComputedStyle(success).display !== 'none');
        """))
    except Exception:
        return False


def _click_captcha(sb) -> bool:
    try:
        sb.uc_gui_click_captcha()
        return True
    except Exception as e:
        log(f"点击 Turnstile 异常: {e}", "WARN")
        return False
    finally:
        ensure_connected(sb)


def wait_for_turnstile_success(sb, timeout: int = 30) -> bool:
    log("等待 Turnstile 验证...")
    start, last_click = time.time(), 0.0
    while time.time() - start < timeout:
        if check_turnstile_solved(sb):
            log("✅ Turnstile 验证成功")
            return True
        if time.time() - last_click > 3:
            _click_captcha(sb)
            last_click = time.time()
            log("点击 Turnstile")
        time.sleep(1)
    log("⏰ Turnstile 验证超时", "WARN")
    return False


MODAL_VISIBLE_JS = """
    var el = document.querySelector('.wisp-start-captcha-modal');
    return !!(el && getComputedStyle(el).display !== 'none');
"""


def _modal_visible(sb) -> bool:
    try:
        ensure_connected(sb)
        return bool(sb.execute_script(MODAL_VISIBLE_JS))
    except Exception:
        return False


def handle_restart_turnstile_modal(sb, timeout: int = 90, wait_modal: int = 20) -> bool:
    """
    处理点击 Start 后的 CF Turnstile 弹窗（.wisp-start-captcha-modal）。
    弹窗从未出现 → 返回 False（由调用方决定是否重试），不再误报成功。
    """
    log("等待 CF Turnstile 重启验证弹窗...")
    appeared = False
    for _ in range(wait_modal):
        if _modal_visible(sb):
            appeared = True
            log("CF Turnstile 弹窗已出现")
            break
        time.sleep(1)
    if not appeared:
        log("CF Turnstile 弹窗未出现", "WARN")
        return False

    start, last_click = time.time(), 0.0
    while time.time() - start < timeout:
        if not _modal_visible(sb):
            log("✅ CF Turnstile 弹窗已关闭，验证完成")
            return True
        if check_turnstile_solved(sb):
            log("Turnstile 已解决，等待弹窗自动关闭...")
            for _ in range(15):
                if not _modal_visible(sb):
                    log("✅ 弹窗已自动关闭")
                    return True
                time.sleep(1)
            return True  # 已解决视为成功，不去点 cancel 之类的按钮
        if time.time() - last_click > 3:
            if _click_captcha(sb):
                log("CF弹窗内点击 Turnstile (uc_gui)")
            last_click = time.time()
        time.sleep(1)

    if not _modal_visible(sb) or check_turnstile_solved(sb):
        log("✅ 超时后验证已完成")
        return True
    log("CF Turnstile 弹窗处理超时", "WARN")
    return False


# ====================== 广告页面处理 ======================
def _dismiss_alert_if_present(sb) -> bool:
    try:
        alert = sb.driver.switch_to.alert
        log(f"发现 Alert 弹窗: {alert.text[:100]}")
        alert.accept()
        log("✅ Alert 弹窗已关闭（点击确定）")
        time.sleep(1)
        return True
    except Exception:
        return False


def _handle_adblocker_page(sb) -> bool:
    log("检测到广告拦截器页面，尝试点击 'Check again'...")
    try:
        sb.execute_script("var b=document.getElementById('recheck-btn'); if(b) b.click();")
        time.sleep(3)
        return True
    except Exception as e:
        log(f"点击 'Check again' 失败: {e}", "WARN")
        return False


def _get_page_situation(sb) -> str:
    url = safe_url(sb)
    if not url:
        return "unknown"
    if "reward-video" in url or "reward_video" in url:
        return "reward"
    try:
        if sb.execute_script("""
            var box = document.querySelector('.check-box');
            var title = document.querySelector('.check-title');
            return !!(box || (title && title.textContent.toLowerCase().includes('adblocker')));
        """):
            return "adblocker"
        if sb.execute_script("return !!(document.getElementById('embedWatchBtn') || document.getElementById('embedPlayBtn'));"):
            return "reward"
    except Exception:
        pass
    return "unknown"


def _try_close_ad_overlay(sb) -> bool:
    """点击广告层的关闭/跳过按钮。长词子串匹配，单字符符号必须全等，且只点按钮类元素。"""
    try:
        ensure_connected(sb)
        clicked = sb.execute_script("""
            var words = ['close', 'skip', 'cancel', '关闭', '跳过', '取消'];
            var symbols = ['×', '✕', 'x'];
            var els = document.querySelectorAll('button, div[role="button"], span[role="button"], [aria-label*="lose"], [aria-label*="kip"]');
            for (var i = 0; i < els.length; i++) {
                var el = els[i];
                if (!el || el.offsetParent === null) continue;
                var own = (el.innerText || el.textContent || '').trim().toLowerCase();
                var meta = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).toLowerCase();
                if (symbols.indexOf(own) !== -1) { try { el.click(); return true; } catch(e) {} }
                var all = own + ' ' + meta;
                for (var k = 0; k < words.length; k++) {
                    if (all.indexOf(words[k]) !== -1) { try { el.click(); return true; } catch(e) {} }
                }
            }
            return false;
        """)
        if clicked:
            log("✅ 已点击广告关闭/取消按钮")
            time.sleep(2)
            return True
    except Exception as e:
        log(f"查找关闭按钮异常: {e}", "WARN")
    return False


def _click_venatus_cancel(sb) -> bool:
    try:
        ensure_connected(sb)
        clicked = sb.execute_script("""
            var buttons = document.querySelectorAll('button, a, [role="button"]');
            for (var i = 0; i < buttons.length; i++) {
                var t = (buttons[i].innerText || buttons[i].textContent || '').trim().toLowerCase();
                if (t === 'cancel' || t === '取消') { buttons[i].click(); return true; }
            }
            for (var j = 0; j < buttons.length; j++) {
                var r = buttons[j].getBoundingClientRect();
                var t2 = (buttons[j].innerText || buttons[j].textContent || '').trim().toLowerCase();
                if (r.top < 80 && r.right > (window.innerWidth - 120) &&
                    (t2.indexOf('cancel') !== -1 || t2.indexOf('close') !== -1 || t2 === '×')) {
                    buttons[j].click(); return true;
                }
            }
            return false;
        """)
        if clicked:
            time.sleep(1)
            return True
    except Exception as e:
        log(f"点击 Cancel 异常: {e}", "WARN")
    return False


def _wait_for_reward_btn_ready(sb, timeout: int = 60) -> bool:
    log(f"等待广告视频加载就绪（最长 {timeout}s）...")
    start = time.time()
    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            ensure_connected(sb)
            r = sb.execute_script("""
                var btn = document.getElementById('embedWatchBtn');
                var panel = document.getElementById('embedPlayBtn');
                var status = document.getElementById('embedStatus');
                if (!btn || !panel) return {ready: false, reason: 'no_element'};
                if (getComputedStyle(panel).display === 'none')
                    return {ready: false, reason: 'panel_hidden', statusText: status ? status.textContent : ''};
                var cs = getComputedStyle(btn);
                return {ready: cs.display !== 'none' && cs.visibility !== 'hidden', reason: 'ok'};
            """)
            if r and r.get("ready"):
                log("✅ 广告已就绪，Watch ad 按钮可点击")
                return True
            if elapsed % 10 == 0:
                log(f"广告加载中... [{elapsed}s] reason={(r or {}).get('reason')}")
        except Exception as e:
            log(f"检查广告就绪状态异常: {e}", "WARN")
        time.sleep(2)
    log(f"⏰ 广告按钮等待超时 ({timeout}s)", "WARN")
    return False


def _click_watch_ad_btn(sb) -> bool:
    log("点击 'Watch ad to continue' 按钮...")
    methods = [
        lambda: sb.execute_script("var b=document.getElementById('embedWatchBtn'); if(!b) return false; b.click(); return true;"),
        lambda: (sb.click("#embedWatchBtn") or True),
        lambda: sb.execute_script("var b=document.getElementById('embedWatchBtn'); if(!b) return false; "
                                  "b.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})); return true;"),
    ]
    for i, m in enumerate(methods, 1):
        try:
            if m():
                log(f"✅ 广告按钮点击成功（方式{i}）")
                time.sleep(1)
                return True
        except Exception as e:
            log(f"广告按钮点击方式{i}失败: {e}", "WARN")
    log("所有广告按钮点击方式均失败", "ERROR")
    return False


def _wait_for_ad_completion(sb, identifier: str, timeout: int = 180) -> bool:
    safe_id = mask_server_id(identifier)
    log(f"广告开始播放，等待完成（最长 {timeout}s）: {safe_id}")
    start = time.time()
    console_path = f"/servers/{identifier}/console"
    time.sleep(5)
    _try_close_ad_overlay(sb)
    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            url = safe_url(sb)
            if elapsed < 60 and elapsed % 8 == 0:
                _try_close_ad_overlay(sb)
            if "rewardDone=1" in url or console_path in url:
                log(f"✅ 广告完成: {safe_id}")
                return True
            if any(k in url for k in ("venatus", "reward-demo", "reward_demo")):
                if elapsed % 15 == 0:
                    log(f"仍在广告中间页 [{elapsed}s]")
                time.sleep(3)
                continue
            try:
                st = sb.execute_script("""
                    var st = document.getElementById('embedStatus');
                    if (!st) return {visible: false, text: ''};
                    return {visible: getComputedStyle(st).display !== 'none', text: st.textContent || ''};
                """)
                if st and st.get("visible"):
                    text = st.get("text", "").lower()
                    if any(kw in text for kw in ("starting", "saving", "returning", "session")):
                        log(f"✅ 广告完成 [embedStatus='{text[:50]}']: {safe_id}")
                        time.sleep(5)
                        return True
            except Exception:
                pass
            if elapsed % 15 == 0:
                log(f"等待广告完成... [{elapsed}s]")
        except Exception as e:
            log(f"广告完成检测异常: {e}", "WARN")
        time.sleep(3)
    log(f"广告等待超时 ({timeout}s): {safe_id}", "WARN")
    return False


def _execute_reward_ad_watch(sb, identifier: str) -> bool:
    log(f"进入广告观看流程: {mask_server_id(identifier)}")
    if not _wait_for_reward_btn_ready(sb, timeout=60):
        if _dismiss_alert_if_present(sb):
            return True
        url = safe_url(sb)
        if "reward-video" not in url and "venatus" not in url:
            return True
        if _click_venatus_cancel(sb):
            time.sleep(2)
            _dismiss_alert_if_present(sb)
        return True
    if _dismiss_alert_if_present(sb):
        return True
    if not _click_watch_ad_btn(sb):
        _click_venatus_cancel(sb)
        _dismiss_alert_if_present(sb)
        return True
    _wait_for_ad_completion(sb, identifier, timeout=180)
    _dismiss_alert_if_present(sb)
    return True


def handle_reward_ad_flow(sb, identifier: str) -> bool:
    """
    点击 Start 后的前置流程：
      venatus 广告页 → Cancel → Alert「No ad available...」→ 确定 → CF 验证
      或出现完整广告观看页。
    """
    log(f"广告流程处理开始: {mask_server_id(identifier)}")
    console_path = f"/servers/{identifier}/console"
    ad_keys = ("venatus", "reward-demo", "reward_demo", "reward-video", "reward")

    navigated_away = False
    for _ in range(8):
        if _dismiss_alert_if_present(sb):
            return True
        url = safe_url(sb)
        if any(k in url for k in ad_keys):
            navigated_away = True
            log(f"已跳转到广告页: {url[:70]}")
            break
        time.sleep(1)

    start, max_wait, cancel_clicked = time.time(), 40, False
    while time.time() - start < max_wait:
        elapsed = int(time.time() - start)
        if _dismiss_alert_if_present(sb):
            return True
        url = safe_url(sb)

        if any(k in url for k in ad_keys):
            if not cancel_clicked:
                if _click_venatus_cancel(sb):
                    cancel_clicked = True
                    log("✅ 已点击 Cancel，等待页面响应...")
                    time.sleep(3)
                    if _dismiss_alert_if_present(sb):
                        return True
                else:
                    _try_close_ad_overlay(sb)
                    time.sleep(2)
            elif elapsed % 5 == 0:
                log(f"已点 Cancel，等待 Alert 或跳转... [{elapsed}s]")
            time.sleep(1)
            continue

        if console_path in url:
            if navigated_away or cancel_clicked or elapsed > 5:
                log("已回到控制台页面，结束广告流程")
                time.sleep(1)
                _dismiss_alert_if_present(sb)
                return True
            time.sleep(1)
            continue

        situation = _get_page_situation(sb)
        if situation == "adblocker":
            _handle_adblocker_page(sb)
            time.sleep(2)
            continue
        if situation == "reward":
            return _execute_reward_ad_watch(sb, identifier)
        time.sleep(1)

    _dismiss_alert_if_present(sb)
    log("广告流程等待超时，继续执行验证", "WARN")
    return True


# ====================== 登录 ======================
def _is_error_page(sb) -> bool:
    try:
        ensure_connected(sb)
        title = (sb.get_title() or "").lower()
        body = (sb.execute_script("return (document.body && document.body.innerText) || '';") or "").lower()
        if any(kw in title for kw in ("500 ", "502 ", "503 ", "520 ", "internal server error", "bad gateway")):
            return True
        if any(kw in body[:600] for kw in ("500 internal server error", "502 bad gateway", "503 service",
                                           "error 520", "error 522", "error 524", "web server is returning an unknown error")):
            return True
        if len(body.strip()) < 30 and not sb.is_element_present("input#email"):
            return True
    except Exception:
        pass
    return False


def login(sb, email: str, password: str) -> bool:
    max_attempts = 4
    form_ok = False
    for attempt in range(1, max_attempts + 1):
        log(f"访问登录页（第 {attempt}/{max_attempts} 次）...")
        try:
            sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=12)
        except Exception as e:
            log(f"打开登录页异常: {e}", "WARN")
        ensure_connected(sb)
        time.sleep(3 + attempt)

        if _is_error_page(sb):
            log(f"检测到错误页/空白页（第 {attempt} 次）", "WARN")
        else:
            try:
                sb.wait_for_element_visible("input#email", timeout=18)
                log("✅ 找到登录表单")
                form_ok = True
                break
            except Exception:
                log(f"未找到登录表单（第 {attempt} 次）", "WARN")

        if attempt < max_attempts:
            restart_warp()
            time.sleep(4)

    if not form_ok:
        log("多次重试后仍无法加载登录页", "ERROR")
        return False

    log("填写登录信息...")
    try:
        sb.type("input#email", email)
        time.sleep(0.6)
        sb.type("input#password", password)
        time.sleep(0.6)
    except Exception as e:
        log(f"填写登录信息失败: {e}", "ERROR")
        return False

    if not wait_for_turnstile_success(sb, timeout=40):
        log("登录 Turnstile 未通过", "ERROR")
        return False

    log("提交登录...")
    try:
        sb.click("button.login-btn")
    except Exception:
        try:
            sb.execute_script('document.querySelector("form#login-form").submit()')
        except Exception as e:
            log(f"提交登录失败: {e}", "ERROR")
            return False

    log("等待跳转到仪表盘...")
    for _ in range(25):
        if "/dashboard" in safe_url(sb):
            log("已跳转到仪表盘")
            break
        if _is_error_page(sb):
            log("跳转过程中出现错误页", "ERROR")
            return False
        time.sleep(1)
    else:
        log("登录后未成功跳转到仪表盘", "ERROR")
        return False

    block_ads_modals(sb)
    log("✅ 登录成功并进入仪表盘")
    return True


# ====================== 浏览器验证流程 ======================
def browser_verify_flow(sb, identifier: str, console_url: str) -> bool:
    """
    点 Start → 广告/Cancel/Alert → 等回控制台（不强制刷新）→ Turnstile 弹窗。
    弹窗没出现则再点一次 Start 重试。返回是否通过验证。
    """
    ensure_connected(sb)
    try:
        if not sb.execute_script(START_BTN_JS):
            log("未找到 Start/Restart 按钮", "WARN")
            return False
    except Exception as e:
        log(f"点击 Start 失败: {e}", "WARN")
        return False
    log("已点击 Start/Restart，进入广告流程")
    time.sleep(2)

    handle_reward_ad_flow(sb, identifier)

    console_path = f"/servers/{identifier}/console"
    for _ in range(10):  # 等页面自己跳回控制台
        if console_path in safe_url(sb):
            break
        time.sleep(1)
    else:
        sb.get(console_url)
        time.sleep(3)

    _dismiss_alert_if_present(sb)
    block_ads_modals(sb)

    passed = handle_restart_turnstile_modal(sb, timeout=60, wait_modal=12)
    if not passed:
        log("未见验证弹窗，再点一次 Start 触发验证...")
        try:
            sb.execute_script(START_BTN_JS)
        except Exception:
            pass
        time.sleep(2)
        _dismiss_alert_if_present(sb)
        passed = handle_restart_turnstile_modal(sb, timeout=60, wait_modal=15)
    return passed


# ====================== 开机 ======================
def restart_server(sb, identifier: str) -> Tuple[bool, str, str]:
    """状态正常 → 跳过；离线/异常 → 开机。"""
    console_url = CONSOLE_URL_TEMPLATE.format(identifier=identifier)
    safe_id = mask_server_id(identifier)
    log("─" * 40)
    log(f"处理服务器: {safe_id}")
    log("─" * 40)

    ensure_connected(sb)
    sb.get(console_url)
    time.sleep(3)
    block_ads_modals(sb)

    info = get_server_info(sb, identifier)
    current_status = str(info.get("current_state") or "").strip() or None
    log(f"当前服务器状态: {current_status or '未知'}")
    log(f"状态接口字段: {sorted(p for p, _ in _flatten(info))}")

    if is_server_running(current_status):
        log(f"✅ 服务器 {safe_id} 状态正常，跳过")
        return True, current_status or "running", "跳过（状态正常）"

    action_desc = "开机"
    log("服务器离线/异常，准备开机")
    gate_ok = ensure_start_gate(sb)
    started = False
    if gate_ok:
        started, msg = api_start_server(sb, identifier)
        if not started:
            log(f"Start API 未成功: {msg}", "WARN")
    if not started:
        log("=== 浏览器验证流程 ===")
        passed = browser_verify_flow(sb, identifier, console_url)
        log(f"浏览器验证{'通过' if passed else '未通过'}")
        if passed:
            # 验证通过通常只是发放资格，显式再调一次 Start
            started, msg = api_start_server(sb, identifier)
            if not started:
                log(f"验证后 Start API 未成功: {msg}，改为轮询状态", "WARN")
        elif not gate_ok:
            if ensure_start_gate(sb):
                started, _ = api_start_server(sb, identifier)
            if not started:
                return False, current_status or "未知", f"{action_desc}失败（无启动资格）"

    # ── 轮询状态 ──
    log(f"开始轮询服务器状态（最长 90 秒）: {safe_id}")
    start_poll, last_status = time.time(), None
    while time.time() - start_poll < 90:
        try:
            status = get_server_status(sb, identifier)
            last_status = status
            if is_server_running(status):
                log(f"✅ 服务器 {safe_id} 状态: {status}，{action_desc}成功")
                return True, status, f"{action_desc}成功"
            log(f"当前状态: {status or '未知'}，5s 后重试...")
        except Exception as e:
            log(f"状态检查异常: {e}", "WARN")
        time.sleep(5)

    status = get_server_status(sb, identifier) or last_status
    if is_server_running(status):
        return True, status, f"{action_desc}成功"
    log(f"轮询超时，最终状态: {status or '未知'}", "ERROR")
    return False, status or "未知", f"{action_desc}失败（超时）"


# ====================== 账号处理 ======================
def build_sb(user_data_dir: str):
    kwargs = dict(uc=True, test=True, locale="en", user_data_dir=user_data_dir,
                  chromium_arg="--disable-blink-features=AutomationControlled")
    # uc_gui_click_captcha 依赖真实/虚拟显示，Linux 无 DISPLAY 时用 xvfb
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        kwargs["xvfb"] = True
    kwargs["headed"] = True
    return SB(**kwargs)


def process_account(idx: int, email: str, password: str, tg_token: str, tg_chat: str) -> bool:
    log("=" * 50)
    log(f"开始处理账号 {idx} | {mask_email(email)}")
    log("=" * 50)

    all_ok = True
    user_data_dir = tempfile.mkdtemp(prefix=f"wisp_usr_{idx}_")
    try:
        with build_sb(user_data_dir) as sb:
            try:
                if not login(sb, email, password):
                    shot = take_screenshot(sb, idx, "login-fail")
                    send_tg_photo(tg_token, tg_chat, shot,
                                  f"❌ 登录失败\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                    return False

                servers = get_servers(sb)
                if not servers:
                    shot = take_screenshot(sb, idx, "no-server")
                    send_tg_photo(tg_token, tg_chat, shot,
                                  f"❌ 未找到服务器\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                    return False

                for si, server_id in enumerate(servers, start=1):
                    success, final_status, action_desc = restart_server(sb, server_id)
                    all_ok = all_ok and success
                    up = extract_uptime_seconds(get_server_info(sb, server_id))
                    if up is not None:
                        uptime_text = fmt_duration(up)
                    elif is_server_running(final_status):
                        uptime_text = "未知（接口无此字段）"
                    else:
                        uptime_text = "未运行"
                    shot = take_screenshot(sb, idx, f"done-{si}" if len(servers) > 1 else "done")
                    caption = (
                        f"{'✅' if success else '❌'} {action_desc}\n\n"
                        f"账号: {mask_email(email)}\n"
                        f"服务器: {mask_server_id(server_id)}\n"
                        f"最终状态: {status_to_chinese(final_status)}\n"
                        f"运行时长: {uptime_text}\n\n"
                        f"Wispbyte Auto Restart"
                    )
                    send_tg_photo(tg_token, tg_chat, shot, caption)
            except Exception as e:
                log(f"账号 {idx} 处理异常: {e}", "ERROR")
                shot = take_screenshot(sb, idx, "exception")
                send_tg_photo(tg_token, tg_chat, shot,
                              f"❌ 脚本异常\n账号: {mask_email(email)}\n信息: {str(e)[:200]}\n\nWispbyte Auto Restart")
                return False
    finally:
        shutil.rmtree(user_data_dir, ignore_errors=True)
    return all_ok


# ====================== 账号加载 ======================
def load_accounts() -> List[Tuple[str, str]]:
    accounts = []
    for i in range(1, 6):
        raw = os.environ.get(f"WISPBYTE_{i}")
        if not raw:
            continue
        parts = raw.split("-----", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            accounts.append((parts[0].strip(), parts[1].strip()))
            log(f"加载账号 WISPBYTE_{i}: {mask_email(parts[0].strip())}")
        else:
            log(f"WISPBYTE_{i} 格式错误，期望 '邮箱-----密码'", "WARN")
    return accounts


def parse_target_emails(raw: str) -> List[str]:
    if not raw or not raw.strip():
        return []
    seen, result = set(), []
    for part in raw.split(","):
        email = part.strip().lower()
        if not email:
            continue
        if "@" not in email:
            log(f"无效的邮箱格式，已跳过: {mask_email(email)}", "WARN")
            continue
        if email in seen:
            continue
        seen.add(email)
        result.append(email)
    return result


# ====================== 入口 ======================
def main():
    tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not tg_token or not tg_chat:
        log("缺少 TG_BOT_TOKEN 或 TG_CHAT_ID，通知功能将不可用", "WARN")

    all_accounts = load_accounts()
    if not all_accounts:
        log("未找到任何有效账号，请检查 Secrets 设置", "ERROR")
        sys.exit(1)

    target_emails = parse_target_emails(os.environ.get("INPUT_ACCOUNTS", ""))
    indexed = [(i, e, p) for i, (e, p) in enumerate(all_accounts, start=1)]
    if target_emails:
        email_map = {e.lower(): (i, e, p) for i, e, p in indexed}
        selected = []
        for t in target_emails:
            if t in email_map:
                selected.append(email_map[t])
            else:
                log(f"邮箱 '{mask_email(t)}' 未在已配置账号中找到，已跳过", "WARN")
        if not selected:
            log("指定的邮箱全部无效，退出", "ERROR")
            sys.exit(1)
        log(f"指定运行账号: {[mask_email(e) for _, e, _ in selected]}")
    else:
        selected = indexed
        log("未指定账号，运行全部账号")

    failed = 0
    for order, (idx, email, password) in enumerate(selected):
        if order > 0:
            restart_warp()
        try:
            ok = process_account(idx, email, password, tg_token, tg_chat)
        except Exception as e:  # SB 启动失败等，不影响后续账号
            log(f"账号 {idx} 致命异常: {e}", "ERROR")
            ok = False
        if not ok:
            failed += 1
        if order < len(selected) - 1:
            time.sleep(5)

    log(f"所有账号处理完毕，失败 {failed}/{len(selected)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
