#!/usr/bin/env python3

import os
import sys
import time
import json
import logging
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional

import requests
from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ====================== 配置 ======================
LOGIN_URL = "https://wispbyte.com/client"
DASHBOARD_URL = "https://wispbyte.com/client/dashboard"
CONSOLE_URL_TEMPLATE = "https://wispbyte.com/client/servers/{identifier}/console"
REWARD_VIDEO_URL = "https://wispbyte.com/client/reward-video"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", str(Path.cwd()))
OUTPUT_DIR = Path(WORKSPACE) / "output/screenshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 上次重启记录文件（配合 Actions cache 可跨运行持久化）
LAST_RESTART_FILE = Path(WORKSPACE) / "output/last_restarts.json"
# 强制重启间隔（天）
FORCE_RESTART_DAYS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("wispbyte_restart")

for _noisy in ("seleniumbase", "selenium", "urllib3", "undetected_chromedriver"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


# ====================== 工具函数 ======================
def mask_email(email: str) -> str:
    """
    账号脱敏格式：a***a@mail.com
    - 本地部分保留首尾字符，中间用 *** 代替
    - 域名完整保留
    """
    if '@' not in email:
        if len(email) <= 2:
            return email[0] + "***" if email else "***"
        return email[0] + "***" + email[-1]
    local, domain = email.split('@', 1)
    if not local:
        masked_local = "***"
    elif len(local) == 1:
        masked_local = local + "***"
    elif len(local) == 2:
        masked_local = local[0] + "***" + local[1]
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


def mask_server_id(identifier: str) -> str:
    if not identifier:
        return "***"
    if len(identifier) <= 4:
        return "***"
    return identifier[:2] + "***" + identifier[-2:]


def log(msg: str, level: str = "INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    logger.info(f"{prefix} {msg}")


def send_tg_photo(token: str, chat_id: str, photo_path: str, caption: str):
    if not token or not chat_id:
        return
    if not photo_path or not os.path.exists(photo_path):
        log(f"截图文件不存在: {photo_path}", "WARN")
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=30
            )
        resp.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 通知异常: {e}", "ERROR")


def restart_warp():
    log("正在重启 WARP 以更换 IP...")
    try:
        old_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"当前 IP: {old_ip}")
    except Exception:
        old_ip = "未知"
    try:
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"],
                       check=False, timeout=30, capture_output=True)
        time.sleep(3)
        try:
            subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
                           check=True, timeout=30, capture_output=True)
        except subprocess.CalledProcessError:
            log("删除注册失败（可能未注册），继续...", "WARN")
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "new"],
                       check=True, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "connect"],
                       check=True, timeout=30, capture_output=True)
        time.sleep(10)
        new_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"WARP 重连成功，新 IP: {new_ip}")
        return True
    except Exception as e:
        log(f"WARP 重连失败: {e}", "ERROR")
        return False


def take_screenshot(sb, account_index: int, suffix: str) -> str:
    timestamp = datetime.now().strftime("%H%M%S")
    filename = f"acc{account_index}-{suffix}-{timestamp}.png"
    filepath = str(OUTPUT_DIR / filename)
    try:
        sb.save_screenshot(filepath)
        log(f"📸 截图保存: {filepath}")
        return filepath
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return ""


# ====================== 广告弹窗 CSS 屏蔽 ======================
def block_ads_modals(sb):
    """屏蔽干扰性广告弹窗（不屏蔽广告本身，只屏蔽无关弹窗）"""
    css = """
    .wisp-offer-modal, .instagram-modal, .qc-cmp2-summary-section {
        display: none !important;
    }
    """
    try:
        sb.execute_script(f'''
            var style = document.createElement('style');
            style.textContent = {json.dumps(css)};
            document.head.appendChild(style);
        ''')
        log("✅ 已注入广告屏蔽 CSS")
    except Exception as e:
        log(f"注入屏蔽 CSS 失败: {e}", "WARN")


# ====================== Turnstile 处理 ======================
def check_turnstile_solved(sb) -> bool:
    """检查当前页面/弹窗中的 Turnstile 是否已完成"""
    try:
        return bool(sb.execute_script('''
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 20) return true;
            var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe && iframe.getAttribute("data-state") === "solved") return true;
            var success = document.getElementById('success');
            return !!(success && getComputedStyle(success).display !== 'none');
        '''))
    except Exception:
        return False


def wait_for_turnstile_success(sb, timeout: int = 30) -> bool:
    """等待并点击登录页 Turnstile"""
    log("等待 Turnstile 验证...")
    start = time.time()
    last_click = 0
    while time.time() - start < timeout:
        if check_turnstile_solved(sb):
            log("✅ Turnstile 验证成功")
            return True
        if time.time() - last_click > 3:
            try:
                sb.uc_gui_click_captcha()
                last_click = time.time()
                log("点击 Turnstile")
            except Exception as e:
                log(f"点击 Turnstile 异常: {e}", "WARN")
        time.sleep(1)
    log("⏰ Turnstile 验证超时", "WARN")
    return False


def handle_restart_turnstile_modal(sb, timeout: int = 90) -> bool:
    """
    处理点击 Start 后弹出的 CF Turnstile 验证弹窗。
    弹窗选择器: .wisp-start-captcha-modal
    """
    log("等待 CF Turnstile 重启验证弹窗...")
    start = time.time()

    # 先等弹窗出现（最多 20 秒）
    modal_appeared = False
    for _ in range(20):
        try:
            exists = sb.execute_script('''
                var el = document.querySelector('.wisp-start-captcha-modal');
                return !!(el && getComputedStyle(el).display !== 'none');
            ''')
            if exists:
                modal_appeared = True
                log("CF Turnstile 弹窗已出现")
                break
        except Exception:
            pass
        time.sleep(1)

    if not modal_appeared:
        # 弹窗从未出现，可能服务器已启动或不需要验证
        log("CF Turnstile 弹窗未出现，可能无需验证", "WARN")
        return True

    last_click = 0
    while time.time() - start < timeout:
        try:
            # 检查弹窗是否还存在
            modal_visible = sb.execute_script('''
                var el = document.querySelector('.wisp-start-captcha-modal');
                return !!(el && getComputedStyle(el).display !== 'none');
            ''')
            if not modal_visible:
                log("✅ CF Turnstile 弹窗已关闭，验证完成")
                return True

            # 检查是否已解决
            if check_turnstile_solved(sb):
                log("Turnstile 已解决，等待弹窗自动关闭...")
                # 等待弹窗自动关闭（最多 15 秒）
                for _ in range(15):
                    closed = sb.execute_script('''
                        var el = document.querySelector('.wisp-start-captcha-modal');
                        return !(el && getComputedStyle(el).display !== 'none');
                    ''')
                    if closed:
                        log("✅ 弹窗已自动关闭")
                        return True
                    time.sleep(1)
                # 尝试手动关闭
                try:
                    sb.execute_script('''
                        var btn = document.querySelector(
                            '.wisp-start-captcha-btn[data-action="cancel"],' +
                            '.wisp-start-captcha-modal button[type="submit"],' +
                            '.wisp-start-captcha-modal .submit-btn'
                        );
                        if (btn) btn.click();
                    ''')
                    time.sleep(2)
                    return True
                except Exception:
                    pass
                return True  # 即使关闭失败，Turnstile已完成视为成功

            # 尝试点击 Turnstile
            now = time.time()
            if now - last_click > 3:
                try:
                    sb.uc_gui_click_captcha()
                    last_click = now
                    log("CF弹窗内点击 Turnstile (uc_gui)")
                except Exception:
                    try:
                        sb.execute_script('''
                            var ts = document.querySelector(
                                '.wisp-start-captcha-modal .cf-turnstile,' +
                                '.wisp-start-captcha-modal iframe'
                            );
                            if (ts) ts.click();
                        ''')
                        last_click = now
                        log("CF弹窗内点击 Turnstile (JS)")
                    except Exception as e:
                        log(f"CF弹窗 Turnstile 点击失败: {e}", "WARN")

        except Exception as e:
            log(f"CF Turnstile 弹窗处理异常: {e}", "WARN")

        time.sleep(1)

    # 超时最终检查
    try:
        if sb.execute_script('return !document.querySelector(".wisp-start-captcha-modal") || getComputedStyle(document.querySelector(".wisp-start-captcha-modal")).display === "none";'):
            log("✅ 超时后弹窗已消失")
            return True
        if check_turnstile_solved(sb):
            log("✅ 超时后 Turnstile 已完成")
            return True
    except Exception:
        pass

    log("CF Turnstile 弹窗处理超时", "WARN")
    return False


# ====================== 广告页面处理 ======================

def _get_page_situation(sb) -> str:
    """
    检测当前页面/弹出情况，返回:
      'adblocker'  - 检测到广告拦截器页面
      'reward'     - 在广告奖励页面（reward-video page）
      'alert'      - 有 JS alert 弹窗（No ad available）
      'console'    - 在控制台页面（正常）
      'unknown'    - 未知
    """
    try:
        current_url = sb.get_current_url()
    except Exception:
        return 'unknown'

    # 检查是否在 reward video 页面
    if "reward-video" in current_url or "reward_video" in current_url:
        return 'reward'

    # 检查 AdBlocker 拦截页
    try:
        adblocker = sb.execute_script('''
            var box = document.querySelector('.check-box');
            var title = document.querySelector('.check-title');
            return !!(box || (title && title.textContent.toLowerCase().includes('adblocker')));
        ''')
        if adblocker:
            return 'adblocker'
    except Exception:
        pass

    # 检查广告按钮是否在当前页面出现（内嵌情况）
    try:
        has_embed_btn = sb.execute_script('''
            return !!(document.getElementById('embedWatchBtn') || 
                      document.getElementById('embedPlayBtn'));
        ''')
        if has_embed_btn:
            return 'reward'
    except Exception:
        pass

    return 'unknown'


def _dismiss_alert_if_present(sb) -> bool:
    """
    处理 JS alert 弹窗（如 'No ad available right now...'）
    返回 True 如果处理了弹窗
    """
    try:
        alert = sb.driver.switch_to.alert
        alert_text = alert.text
        log(f"发现 Alert 弹窗: {alert_text[:100]}")
        alert.accept()
        log("✅ Alert 弹窗已关闭（点击确定）")
        time.sleep(1)
        return True
    except Exception:
        return False


def _handle_adblocker_page(sb) -> bool:
    """
    处理广告拦截器检测页面。
    点击 'Check again' 按钮。
    """
    log("检测到广告拦截器页面，尝试点击 'Check again'...")
    try:
        sb.execute_script('''
            var btn = document.getElementById('recheck-btn');
            if (btn) btn.click();
        ''')
        log("✅ 已点击 'Check again'")
        time.sleep(3)
        return True
    except Exception as e:
        log(f"点击 'Check again' 失败: {e}", "WARN")
        return False


def _wait_for_reward_btn_ready(sb, timeout: int = 90) -> bool:
    """
    等待广告奖励页面的 embedWatchBtn 变为可点击状态。
    就绪条件：
      - embedPlayBtn 显示 (display: flex)
      - embedStatus 已隐藏（说明广告加载完成）
    """
    log(f"等待广告视频加载就绪（最长 {timeout}s）...")
    start = time.time()

    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            result = sb.execute_script('''
                var btn = document.getElementById('embedWatchBtn');
                var panel = document.getElementById('embedPlayBtn');
                var status = document.getElementById('embedStatus');

                if (!btn || !panel) return {ready: false, reason: 'no_element'};

                var panelDisplay = window.getComputedStyle(panel).display;
                var btnDisplay = window.getComputedStyle(btn).display;
                var btnVis = window.getComputedStyle(btn).visibility;

                // panel 必须是 flex（showEmbedPlayButton 设置的）
                if (panelDisplay === 'none') {
                    // 检查 embedStatus 当前内容
                    var statusText = status ? status.textContent : '';
                    return {ready: false, reason: 'panel_hidden', statusText: statusText};
                }

                return {
                    ready: btnDisplay !== 'none' && btnVis !== 'hidden',
                    reason: 'ok'
                };
            ''')

            if result and result.get('ready'):
                log("✅ 广告已就绪，Watch ad 按钮可点击")
                return True
            else:
                reason = result.get('reason', '?') if result else '?'
                status_text = result.get('statusText', '') if result else ''
                if elapsed % 10 == 0:
                    log(f"广告加载中... [{elapsed}s] reason={reason} status='{status_text[:60]}'")

        except Exception as e:
            log(f"检查广告就绪状态异常: {e}", "WARN")

        time.sleep(2)

    log(f"⏰ 广告按钮等待超时 ({timeout}s)", "WARN")
    return False


def _click_watch_ad_btn(sb) -> bool:
    """
    点击 embedWatchBtn 按钮。
    使用多种方式确保点击成功。
    """
    log("点击 'Watch ad to continue' 按钮...")
    methods = [
        # 方式1: JS click（最可靠，绕过遮挡）
        lambda: sb.execute_script('''
            var btn = document.getElementById('embedWatchBtn');
            if (!btn) return false;
            btn.click();
            return true;
        '''),
        # 方式2: SeleniumBase click
        lambda: (sb.click('#embedWatchBtn') or True),
        # 方式3: JS dispatchEvent
        lambda: sb.execute_script('''
            var btn = document.getElementById('embedWatchBtn');
            if (!btn) return false;
            btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            return true;
        '''),
    ]

    for i, method in enumerate(methods, 1):
        try:
            result = method()
            if result:
                log(f"✅ 广告按钮点击成功（方式{i}）")
                time.sleep(1)
                return True
        except Exception as e:
            log(f"广告按钮点击方式{i}失败: {e}", "WARN")

    log("❌ 所有广告按钮点击方式均失败", "ERROR")
    return False


def _try_close_ad_overlay(sb) -> bool:
    """
    尝试点击广告层中的关闭 / 取消 / Skip 按钮。
    返回 True 表示成功点击了某个关闭按钮。
    """
    close_selectors = [
        # 常见关闭按钮
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        'button[title="Close"]',
        'button[title="close"]',
        '.close-button',
        '.ad-close',
        '.close-btn',
        '#close-btn',
        'button.close',
        '[class*="close-button"]',
        '[class*="closeBtn"]',
        '[class*="CloseButton"]',
        # Skip / Cancel
        'button[aria-label="Skip"]',
        'button[aria-label="skip"]',
        '.skip-button',
        '.skip-btn',
        'button.skip',
        '[class*="skip"]',
        # 通用文本按钮
        'button',
        'a',
        'div[role="button"]',
    ]
    try:
        clicked = sb.execute_script('''
            var keywords = ['close', 'skip', 'cancel', '关闭', '跳过', '取消', '×', '✕', 'x'];
            var candidates = document.querySelectorAll(
                'button, a, div[role="button"], span[role="button"], [class*="close"], [class*="skip"], [aria-label*="lose"], [aria-label*="kip"]'
            );
            for (var i = 0; i < candidates.length; i++) {
                var el = candidates[i];
                if (!el || el.offsetParent === null) continue;  // 不可见
                var text = ((el.innerText || el.textContent || '') + ' ' +
                            (el.getAttribute('aria-label') || '') + ' ' +
                            (el.getAttribute('title') || '') + ' ' +
                            (el.className || '')).toLowerCase();
                for (var k = 0; k < keywords.length; k++) {
                    if (text.indexOf(keywords[k]) !== -1) {
                        try { el.click(); return true; } catch(e) {}
                    }
                }
            }
            // 尝试右上角小关闭图标
            var svgs = document.querySelectorAll('svg, [class*="icon-close"], [class*="icon_close"]');
            for (var j = 0; j < svgs.length; j++) {
                var parent = svgs[j].closest('button, a, div[role="button"]') || svgs[j].parentElement;
                if (parent && parent.offsetParent !== null) {
                    try { parent.click(); return true; } catch(e) {}
                }
            }
            return false;
        ''')
        if clicked:
            log("✅ 已点击广告关闭/取消按钮")
            time.sleep(2)
            return True
    except Exception as e:
        log(f"查找关闭按钮异常: {e}", "WARN")
    return False


def _wait_for_ad_completion(sb, identifier: str, timeout: int = 300) -> bool:
    """
    等待广告观看完成。
    改进点：
      1. 点击 Watch ad 后先强制等待至少 5 秒
      2. 期间及之后持续尝试点击关闭/取消按钮
      3. 忽略 venatus-reward-demo 等中间跳转页，不误判为完成
      4. 真正回到控制台或出现 rewardDone 才算完成
    """
    safe_id = mask_server_id(identifier)
    log(f"广告开始播放，等待完成（最长 {timeout}s）: {safe_id}")
    start = time.time()
    console_path = f"/servers/{identifier}/console"

    # 强制先等待 5 秒，给广告弹出时间，并尝试找关闭按钮
    log("等待 5 秒让广告弹出，并查找关闭/取消按钮...")
    time.sleep(5)
    _try_close_ad_overlay(sb)

    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            current_url = sb.get_current_url() or ""

            # 持续尝试关闭广告层
            if elapsed < 60 and elapsed % 8 == 0:
                _try_close_ad_overlay(sb)

            # 信号1: URL 含 rewardDone=1（真正完成）
            if "rewardDone=1" in current_url:
                log(f"✅ 广告完成 [rewardDone]: {safe_id}")
                return True

            # 信号2: 真正回到控制台页面（必须包含 /servers/{id}/console）
            if console_path in current_url:
                log(f"✅ 广告完成 [回到控制台]: {safe_id}")
                return True

            # 忽略中间广告页（venatus 等），不算完成
            if "venatus" in current_url or "reward-demo" in current_url or "reward_demo" in current_url:
                if elapsed % 15 == 0:
                    log(f"仍在广告中间页 [{elapsed}s]: {current_url[:70]}")
                time.sleep(3)
                continue

            # 信号3: embedStatus 显示完成文字
            try:
                status_info = sb.execute_script('''
                    var st = document.getElementById('embedStatus');
                    if (!st) return {visible: false, text: ''};
                    var display = window.getComputedStyle(st).display;
                    return {
                        visible: display !== 'none',
                        text: st.textContent || ''
                    };
                ''')
                if status_info and status_info.get('visible'):
                    text = status_info.get('text', '').lower()
                    if any(kw in text for kw in ['starting', 'saving', 'returning', 'session']):
                        log(f"✅ 广告完成 [embedStatus='{text[:50]}']: {safe_id}")
                        time.sleep(5)
                        return True
            except Exception:
                pass

            # 信号4: 离开 reward 相关页面，且已经过足够时间，回到正常页面
            if elapsed > 20:
                if ("reward-video" not in current_url and
                    "venatus" not in current_url and
                    "reward-demo" not in current_url and
                    "reward_demo" not in current_url):
                    situation = _get_page_situation(sb)
                    if situation == 'console' or (identifier in current_url and "client" in current_url):
                        log(f"✅ 广告完成 [页面已回到正常页: {current_url[:70]}]: {safe_id}")
                        return True

            if elapsed % 15 == 0:
                log(f"等待广告完成... [{elapsed}s] URL={current_url[:60]}")

        except Exception as e:
            log(f"广告完成检测异常: {e}", "WARN")

        time.sleep(3)

    log(f"⚠️ 广告等待超时 ({timeout}s): {safe_id}", "WARN")
    return False


def handle_reward_ad_flow(sb, identifier: str, console_url: str) -> bool:
    """
    完整广告/验证前置流程（根据实际页面行为）：
      1. 点击 Start 后可能跳转到 venatus 广告页（右上角有 Cancel）
      2. 点击 Cancel 或等待后，可能出现 Alert：
         "No ad available right now — complete a quick verification to start."
      3. 点击 Alert 的「确定」后，进入 CF Turnstile 验证
      4. 也可能直接出现完整广告观看流程

    返回 True 表示可以继续执行 CF 验证流程
    """
    safe_id = mask_server_id(identifier)
    log(f"广告流程处理开始: {safe_id}")
    console_path = f"/servers/{identifier}/console"

    # 最多处理 45 秒
    start = time.time()
    max_wait = 45
    cancel_clicked = False

    while time.time() - start < max_wait:
        elapsed = int(time.time() - start)

        # ① 优先处理 JS Alert（"No ad available..."）
        if _dismiss_alert_if_present(sb):
            log("✅ 已点击 Alert「确定」，准备进入 CF 验证")
            time.sleep(1)
            return True

        current_url = ""
        try:
            current_url = sb.get_current_url() or ""
        except Exception:
            pass

        # ② 已回到控制台 → 结束广告流程，交给 CF 处理
        if console_path in current_url:
            log("已回到控制台页面，结束广告流程")
            # 再检查一次是否有 Alert
            time.sleep(1)
            if _dismiss_alert_if_present(sb):
                log("✅ 回到控制台后处理了 Alert")
            return True

        # ③ 在 venatus / reward 广告页 → 等待几秒后点右上角 Cancel
        if ("venatus" in current_url or "reward-demo" in current_url or
            "reward_demo" in current_url or "reward-video" in current_url):
            if not cancel_clicked and elapsed >= 3:
                log("检测到广告页，尝试点击右上角 Cancel 按钮...")
                if _click_venatus_cancel(sb):
                    cancel_clicked = True
                    log("✅ 已点击 Cancel，等待页面响应...")
                    time.sleep(3)
                    # 点击 Cancel 后常会弹出 Alert
                    if _dismiss_alert_if_present(sb):
                        log("✅ Cancel 后处理了 Alert「确定」")
                        return True
                else:
                    # 兜底：用通用关闭逻辑
                    _try_close_ad_overlay(sb)
            if elapsed % 10 == 0:
                log(f"仍在广告页 [{elapsed}s]: {current_url[:60]}")
            time.sleep(1)
            continue

        # ④ 其他情况（adblocker 等）
        situation = _get_page_situation(sb)
        if situation == 'adblocker':
            log("检测到广告拦截器页面")
            _handle_adblocker_page(sb)
            time.sleep(2)
            continue

        if situation == 'reward':
            # 有完整广告观看按钮的情况，走原逻辑
            log("检测到完整广告观看页，尝试处理...")
            return _execute_reward_ad_watch(sb, identifier)

        time.sleep(1)

    # 超时收尾
    if _dismiss_alert_if_present(sb):
        log("✅ 超时后处理了 Alert")
        return True
    log("广告流程等待超时，继续执行 CF 验证", "WARN")
    return True


def _click_venatus_cancel(sb) -> bool:
    """
    专门点击 venatus 广告页右上角的 Cancel 按钮。
    """
    try:
        # 优先精确匹配
        clicked = sb.execute_script('''
            // 1. 文本精确为 Cancel 的按钮
            var buttons = document.querySelectorAll('button, a, [role="button"]');
            for (var i = 0; i < buttons.length; i++) {
                var el = buttons[i];
                var text = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (text === 'cancel' || text === '取消') {
                    el.click();
                    return true;
                }
            }
            // 2. 右上角位置的按钮
            for (var j = 0; j < buttons.length; j++) {
                var b = buttons[j];
                var rect = b.getBoundingClientRect();
                var text2 = (b.innerText || b.textContent || '').trim().toLowerCase();
                if (rect.top < 80 && rect.right > (window.innerWidth - 120) &&
                    (text2.indexOf('cancel') !== -1 || text2.indexOf('close') !== -1 || text2 === '×')) {
                    b.click();
                    return true;
                }
            }
            return false;
        ''')
        if clicked:
            time.sleep(1)
            return True
    except Exception as e:
        log(f"点击 Cancel 异常: {e}", "WARN")
    return False


def _execute_reward_ad_watch(sb, identifier: str) -> bool:
    """
    在 reward-video 页面执行完整广告观看流程（有真实广告可看时）。
    返回 True 表示广告流程结束（无论成功与否都应继续CF验证）
    """
    safe_id = mask_server_id(identifier)
    log(f"进入广告观看流程: {safe_id}")

    # 等待 Watch ad 按钮就绪
    btn_ready = _wait_for_reward_btn_ready(sb, timeout=60)

    if not btn_ready:
        if _dismiss_alert_if_present(sb):
            log("✅ 广告按钮未就绪但检测到 Alert（无广告），继续CF验证")
            return True
        current_url = sb.get_current_url() or ""
        if "reward-video" not in current_url and "venatus" not in current_url:
            log(f"广告页面已自动跳转: {current_url[:80]}")
            return True
        # 尝试点 Cancel
        if _click_venatus_cancel(sb):
            time.sleep(2)
            _dismiss_alert_if_present(sb)
            return True
        log("广告按钮未就绪，继续CF验证", "WARN")
        return True

    if _dismiss_alert_if_present(sb):
        log("✅ 点击前检测到 Alert（无广告），继续CF验证")
        return True

    if not _click_watch_ad_btn(sb):
        log("广告按钮点击失败，尝试 Cancel...", "WARN")
        _click_venatus_cancel(sb)
        _dismiss_alert_if_present(sb)
        return True

    _wait_for_ad_completion(sb, identifier, timeout=180)
    _dismiss_alert_if_present(sb)
    log(f"广告流程结束: {safe_id}")
    return True


# ====================== 登录流程 ======================
def _is_error_page(sb) -> bool:
    """检测是否为 500 / 502 / 503 / nginx 错误页或空白页。"""
    try:
        title = (sb.get_title() or "").lower()
        body = sb.execute_script("return (document.body && document.body.innerText) || '';") or ""
        body_lower = body.lower()
        if any(kw in title for kw in ["500", "502", "503", "error", "internal server"]):
            return True
        if any(kw in body_lower for kw in [
            "500 internal server error", "502 bad gateway", "503 service",
            "nginx/", "internal server error", "cloudflare"
        ]):
            return True
        # 页面几乎空白且无登录表单
        if len(body.strip()) < 30 and not sb.is_element_present("input#email"):
            return True
    except Exception:
        pass
    return False


def login(sb, email: str, password: str) -> bool:
    """
    登录流程，针对 500 错误页和加载失败增加多次重试。
    """
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        log(f"访问登录页（第 {attempt}/{max_attempts} 次）...")
        try:
            sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=12)
        except Exception as e:
            log(f"打开登录页异常: {e}", "WARN")

        time.sleep(3 + attempt)  # 递增等待

        # 检测错误页
        if _is_error_page(sb):
            log(f"检测到错误页/空白页（第 {attempt} 次），准备重试...", "WARN")
            if attempt < max_attempts:
                # 尝试更换 IP
                restart_warp()
                time.sleep(5)
                continue
            else:
                log("多次重试后仍为错误页，登录失败", "ERROR")
                return False

        # 等待登录表单
        try:
            sb.wait_for_element_visible('input#email', timeout=18)
            log("✅ 找到登录表单")
            break
        except TimeoutException:
            log(f"未找到登录表单（第 {attempt} 次）", "WARN")
            if attempt < max_attempts:
                restart_warp()
                time.sleep(4)
                continue
            else:
                log("多次重试后仍未找到登录表单", "ERROR")
                return False
    else:
        log("登录页加载失败", "ERROR")
        return False

    log("填写登录信息...")
    try:
        sb.type('input#email', email)
        time.sleep(0.6)
        sb.type('input#password', password)
        time.sleep(0.6)
    except Exception as e:
        log(f"填写登录信息失败: {e}", "ERROR")
        return False

    if not wait_for_turnstile_success(sb, timeout=40):
        log("登录 Turnstile 未通过", "ERROR")
        return False

    log("提交登录...")
    try:
        sb.click('button.login-btn')
    except Exception:
        try:
            sb.execute_script('document.querySelector("form#login-form").submit()')
        except Exception as e:
            log(f"提交登录失败: {e}", "ERROR")
            return False

    log("等待跳转到仪表盘...")
    for _ in range(20):
        current = sb.get_current_url()
        if "/dashboard" in current or "/client/dashboard" in current:
            log("已跳转到仪表盘")
            break
        # 如果又跳回错误页，也算失败
        if _is_error_page(sb):
            log("跳转过程中出现错误页", "ERROR")
            return False
        time.sleep(1)
    else:
        log("登录后未成功跳转到仪表盘", "ERROR")
        return False

    block_ads_modals(sb)

    DASHBOARD_SELECTORS = [
        'div.server-list', 'div.servers-container', 'div.card',
        'div.server-card', 'table', 'main', 'section', '#app',
    ]
    dashboard_ready = False
    for sel in DASHBOARD_SELECTORS:
        try:
            sb.wait_for_element_present(sel, timeout=3)
            dashboard_ready = True
            log(f"✅ 仪表盘已就绪 ({sel})")
            break
        except Exception:
            continue

    if not dashboard_ready:
        try:
            body_len = sb.execute_script("return document.body.innerText.length")
            if body_len and int(body_len) > 100:
                log("✅ 仪表盘页面有内容，继续执行")
                dashboard_ready = True
        except Exception:
            pass

    if not dashboard_ready:
        log("仪表盘结构未识别，但继续执行", "WARN")

    log("✅ 登录成功并进入仪表盘")
    return True


# ====================== 获取服务器列表 ======================
def get_servers(sb) -> List[str]:
    log("通过 fetch 请求服务器列表...")
    try:
        result = sb.execute_async_script('''
            var callback = arguments[arguments.length - 1];
            fetch('/client/api/servers/status', {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.servers) {
                    callback(data.servers.map(function(s) { return s.identifier; }));
                } else {
                    callback([]);
                }
            })
            .catch(function(err) { callback([]); });
        ''')
        if result and isinstance(result, list):
            ids = [str(i) for i in result if i]
            if ids:
                masked = [mask_server_id(i) for i in ids]
                log(f"成功获取服务器列表，共 {len(ids)} 台: {masked}")
                return ids
    except Exception as e:
        log(f"fetch 请求失败: {e}", "ERROR")

    # 备用 DOM 提取
    try:
        dom_ids = sb.execute_script('''
            var cards = document.querySelectorAll(
                '[data-server-id], .server-card, .server-item'
            );
            return Array.from(cards).map(function(el) {
                return el.getAttribute('data-server-id') || el.id;
            }).filter(Boolean);
        ''')
        if dom_ids:
            masked = [mask_server_id(i) for i in dom_ids]
            log(f"从 DOM 提取到服务器，共 {len(dom_ids)} 台: {masked}")
            return list(dom_ids)
    except Exception as e:
        log(f"DOM 提取失败: {e}", "WARN")

    log("未能获取任何服务器标识符", "ERROR")
    return []


def get_server_status(sb, identifier: str) -> Optional[str]:
    """
    通过 API 获取指定服务器的当前状态。
    返回 current_state 字符串（如 'running', 'offline', 'starting' 等），
    失败时返回 None。
    """
    status_script = f'''
        var callback = arguments[arguments.length - 1];
        var serverId = {json.dumps(identifier)};
        fetch('/client/api/servers/status', {{
            method: 'GET',
            headers: {{ 'Accept': 'application/json' }}
        }})
        .then(function(res) {{ return res.json(); }})
        .then(function(data) {{
            var server = (data.servers || []).find(function(s) {{
                return s.identifier === serverId;
            }});
            callback(server ? (server.current_state || null) : null);
        }})
        .catch(function() {{ callback(null); }});
    '''
    try:
        status = sb.execute_async_script(status_script)
        return str(status).strip() if status else None
    except Exception as e:
        log(f"获取服务器状态异常: {e}", "WARN")
        return None


def is_server_running(status: Optional[str]) -> bool:
    """判断状态是否表示服务器已开机运行。"""
    if not status:
        return False
    return 'running' in status.lower()


def status_to_chinese(status: Optional[str]) -> str:
    """将服务器状态翻译为中文。"""
    if not status:
        return "未知"
    s = status.lower().strip()
    mapping = {
        "running": "运行中",
        "offline": "离线",
        "stopped": "已停止",
        "starting": "启动中",
        "stopping": "停止中",
        "installing": "安装中",
        "suspended": "已暂停",
        "unknown": "未知",
    }
    for key, cn in mapping.items():
        if key in s:
            return cn
    return status  # 未知状态保留原文


def load_last_restarts() -> dict:
    """加载各服务器上次成功操作的时间戳。"""
    try:
        if LAST_RESTART_FILE.exists():
            with open(LAST_RESTART_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        log(f"读取上次重启记录失败: {e}", "WARN")
    return {}


def save_last_restart(identifier: str):
    """记录服务器本次成功操作时间。"""
    try:
        data = load_last_restarts()
        data[identifier] = datetime.now().isoformat(timespec="seconds")
        LAST_RESTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LAST_RESTART_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"已记录服务器 {mask_server_id(identifier)} 操作时间")
    except Exception as e:
        log(f"保存重启记录失败: {e}", "WARN")


def should_force_restart(identifier: str) -> bool:
    """
    判断是否已超过强制重启间隔（默认 5 天）。
    无记录时视为需要强制重启。
    """
    data = load_last_restarts()
    last_str = data.get(identifier)
    if not last_str:
        log(f"服务器 {mask_server_id(identifier)} 无历史重启记录，需要执行操作")
        return True
    try:
        last_dt = datetime.fromisoformat(last_str)
        days_passed = (datetime.now() - last_dt).total_seconds() / 86400
        if days_passed >= FORCE_RESTART_DAYS:
            log(f"服务器 {mask_server_id(identifier)} 距上次操作已 {days_passed:.1f} 天，达到 {FORCE_RESTART_DAYS} 天强制重启条件")
            return True
        log(f"服务器 {mask_server_id(identifier)} 距上次操作仅 {days_passed:.1f} 天，未达强制重启间隔")
        return False
    except Exception as e:
        log(f"解析上次重启时间失败: {e}，将强制执行", "WARN")
        return True


# ====================== 重启 / 启动服务器（完整流程）======================
def restart_server(sb, identifier: str) -> Tuple[bool, str, str]:
    """
    智能启动/重启流程。
    返回: (是否成功, 最终状态描述, 执行的动作说明)
    """
    console_url = CONSOLE_URL_TEMPLATE.format(identifier=identifier)
    safe_id = mask_server_id(identifier)
    log(f"{'─'*40}")
    log(f"处理服务器: {safe_id}")
    log(f"{'─'*40}")

    # ── Step 1: 导航到控制台 ──
    log(f"导航到控制台: {safe_id}")
    sb.get(console_url)
    time.sleep(4)
    block_ads_modals(sb)

    # ── Step 2: 检查状态并决定是否操作 ──
    current_status = get_server_status(sb, identifier)
    log(f"当前服务器状态: {current_status or '未知'}")

    running = is_server_running(current_status)
    force = should_force_restart(identifier)

    if running and not force:
        log(f"✅ 服务器 {safe_id} 状态正常，且未到 {FORCE_RESTART_DAYS} 天强制重启周期，跳过")
        return True, current_status or "running", "跳过（状态正常）"

    if running and force:
        action = "Restart"
        preferred_selectors = [
            ('button#restart-btn', 'Restart'),
            ('button#start-btn', 'Start'),
        ]
        log(f"服务器状态正常，但已到 {FORCE_RESTART_DAYS} 天强制重启周期，将执行重启")
        action_desc = "强制重启"
    else:
        # 状态异常（offline / stopped / starting / unknown 等）
        action = "Start"
        preferred_selectors = [
            ('button#start-btn', 'Start'),
            ('button#restart-btn', 'Restart'),
        ]
        log("服务器状态异常，将执行开机/重启操作")
        action_desc = "开机/重启"

    # ── Step 3: 点击对应按钮 ──
    action_btn = None
    used_name = action
    for btn_sel, btn_name in preferred_selectors:
        try:
            action_btn = sb.wait_for_element_visible(btn_sel, timeout=8)
            used_name = btn_name
            log(f"找到 {btn_name} 按钮")
            break
        except Exception:
            continue

    if not action_btn:
        log("未找到 Start 或 Restart 按钮", "ERROR")
        return False, current_status or "未知", f"失败（未找到按钮，原计划{action_desc}）"

    try:
        action_btn.click()
        log(f"✅ 已点击 {used_name} 按钮")
    except Exception:
        try:
            js_selector = "#restart-btn" if action == "Restart" else "#start-btn"
            sb.execute_script(
                f"var b = document.querySelector('{js_selector}') || "
                f"document.querySelector('#start-btn, #restart-btn');"
                f"if (b) b.click();"
            )
            log(f"✅ 已通过 JS 点击 {used_name} 按钮")
        except Exception as e:
            log(f"点击 {used_name} 失败: {e}", "ERROR")
            return False, current_status or "未知", f"失败（点击按钮异常，原计划{action_desc}）"

    # 等待页面响应
    time.sleep(3)

    # ── Step 4: 处理广告流程 ──
    log("=== 开始处理广告流程 ===")
    handle_reward_ad_flow(sb, identifier, console_url)
    log("=== 广告流程处理完毕 ===")

    # ── Step 5: 确保回到控制台页面 ──
    time.sleep(2)
    current_url = sb.get_current_url()
    if identifier not in current_url or "reward" in current_url:
        log(f"当前不在控制台页面（{current_url[:80]}），重新导航...")
        sb.get(console_url)
        time.sleep(4)
        block_ads_modals(sb)
    else:
        log("当前在控制台页面，无需重新导航")
        block_ads_modals(sb)

    # ── Step 6: 处理 CF Turnstile 验证弹窗 ──
    log("=== 开始处理 CF Turnstile 验证 ===")
    cf_result = handle_restart_turnstile_modal(sb, timeout=90)
    if not cf_result:
        log("CF Turnstile 验证失败", "WARN")
    else:
        log("=== CF Turnstile 验证完成 ===")

    block_ads_modals(sb)

    # ── Step 7: 轮询服务器状态 ──
    log(f"开始轮询服务器状态（最长 90 秒）: {safe_id}")
    poll_timeout = 90
    poll_interval = 5
    start_poll = time.time()
    last_status = None

    while time.time() - start_poll < poll_timeout:
        try:
            status = get_server_status(sb, identifier)
            last_status = status
            if is_server_running(status):
                log(f"✅ 服务器 {safe_id} 状态: {status}，{action} 成功！")
                save_last_restart(identifier)
                return True, status, f"{action_desc}成功"
            log(f"当前状态: {status or '未知'}，{poll_interval}s 后重试...")
        except Exception as e:
            log(f"状态检查异常: {e}", "WARN")
        time.sleep(poll_interval)

    # 最终检查
    try:
        status = get_server_status(sb, identifier)
        last_status = status or last_status
        if is_server_running(status):
            log(f"✅ 最终检查成功: {safe_id} 状态: {status}")
            save_last_restart(identifier)
            return True, status, f"{action_desc}成功"
        log(f"❌ 轮询超时，最终状态: {last_status or '未知'}", "ERROR")
        return False, last_status or "未知", f"{action_desc}失败（超时）"
    except Exception as e:
        log(f"最终状态检查失败: {e}", "ERROR")
        return False, last_status or "未知", f"{action_desc}失败（异常）"


# ====================== 账号处理 ======================
def process_account(idx: int, email: str, password: str, tg_token: str, tg_chat: str):
    log(f"{'='*50}")
    log(f"开始处理账号 {idx} | {mask_email(email)}")
    log(f"{'='*50}")

    user_data_dir = tempfile.mkdtemp(prefix=f"wisp_usr_{idx}_")
    with SB(uc=True, test=True, locale="en", headed=False,
            user_data_dir=user_data_dir,
            chromium_arg="--disable-blink-features=AutomationControlled") as sb:
        try:
            if not login(sb, email, password):
                screenshot = take_screenshot(sb, idx, "login-fail")
                send_tg_photo(tg_token, tg_chat, screenshot,
                              f"❌ 登录失败\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                return

            servers = get_servers(sb)
            if not servers:
                screenshot = take_screenshot(sb, idx, "no-server")
                send_tg_photo(tg_token, tg_chat, screenshot,
                              f"❌ 未找到服务器\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                return

            for si, server_id in enumerate(servers, start=1):
                success, final_status, action_desc = restart_server(sb, server_id)
                suffix = f"done-{si}" if len(servers) > 1 else "done"
                screenshot = take_screenshot(sb, idx, suffix)
                status_icon = "✅" if success else "❌"
                caption = (
                    f"{status_icon} {action_desc}\n\n"
                    f"账号: {mask_email(email)}\n"
                    f"服务器: {server_id}\n"
                    f"最终状态: {status_to_chinese(final_status)}\n\n"
                    f"Wispbyte Auto Restart"
                )
                send_tg_photo(tg_token, tg_chat, screenshot, caption)

        except Exception as e:
            log(f"账号 {idx} 处理异常: {e}", "ERROR")
            screenshot = take_screenshot(sb, idx, "exception")
            send_tg_photo(tg_token, tg_chat, screenshot,
                          f"❌ 脚本异常\n账号: {mask_email(email)}\n信息: {str(e)[:200]}\n\nWispbyte Auto Restart")


# ====================== 账号加载 ======================
def load_accounts() -> List[Tuple[str, str]]:
    accounts = []
    for i in range(1, 6):
        raw = os.environ.get(f"WISPBYTE_{i}")
        if not raw:
            continue
        parts = raw.split("-----")
        if len(parts) >= 2:
            email = parts[0].strip()
            password = parts[1].strip()
            if email and password:
                accounts.append((email, password))
                log(f"加载账号 WISPBYTE_{i}: {mask_email(email)}")
            else:
                log(f"WISPBYTE_{i} 格式不正确（邮箱或密码为空）", "WARN")
        else:
            log(f"WISPBYTE_{i} 格式错误，期望 '邮箱-----密码'", "WARN")
    return accounts


def parse_target_emails(raw: str) -> List[str]:
    if not raw or not raw.strip():
        return []
    seen = set()
    result = []
    for part in raw.split(","):
        email = part.strip().lower()
        if not email:
            continue
        if "@" not in email:
            log(f"无效的邮箱格式: '{email}'，已跳过", "WARN")
            continue
        if email in seen:
            log(f"重复邮箱: '{email}'，已跳过", "WARN")
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

    target_raw = os.environ.get("INPUT_ACCOUNTS", "").strip()
    target_emails = parse_target_emails(target_raw)

    if target_emails:
        all_email_map = {
            email.lower(): (idx, email, password)
            for idx, (email, password) in enumerate(all_accounts, start=1)
        }
        selected = []
        for target in target_emails:
            if target in all_email_map:
                selected.append(all_email_map[target])
            else:
                log(f"邮箱 '{mask_email(target)}' 未在已配置账号中找到，已跳过", "WARN")
        if not selected:
            log("指定的邮箱全部无效，退出", "ERROR")
            sys.exit(1)
        log(f"指定运行账号: {[mask_email(e) for _, e, _ in selected]}")
    else:
        selected = [(idx, email, password)
                    for idx, (email, password) in enumerate(all_accounts, start=1)]
        log("未指定账号，运行全部账号")

    for run_order, (idx, email, password) in enumerate(selected):
        if run_order > 0:
            restart_warp()
        process_account(idx, email, password, tg_token, tg_chat)
        if run_order < len(selected) - 1:
            time.sleep(5)

    log("所有账号处理完毕")


if __name__ == "__main__":
    main()
