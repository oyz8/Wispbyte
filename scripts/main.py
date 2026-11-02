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
from selenium.webdriver.common.by import By
from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ====================== 配置 ======================
LOGIN_URL = "https://wispbyte.com/client"
CONSOLE_URL_TEMPLATE = "https://wispbyte.com/client/servers/{identifier}/console"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", str(Path.cwd()))
OUTPUT_DIR = Path(WORKSPACE) / "output/screenshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

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
    if '@' not in email:
        return email[:1] + "***"
    local, domain = email.split('@', 1)
    masked_local = local[:1] + "***" if local else "***"
    if '.' in domain:
        parts = domain.split('.')
        tld = parts[-1]
        masked_domain = f"{domain[0]}***.{tld}"
    else:
        masked_domain = domain[:1] + "***"
    return f"{masked_local}@{masked_domain}"


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
        pass
    try:
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"],
                       check=False, timeout=30, capture_output=True)
        time.sleep(3)
        try:
            subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
                           check=True, timeout=30, capture_output=True)
        except subprocess.CalledProcessError:
            pass
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


# ====================== 广告弹窗 / 引导弹窗屏蔽 ======================
def block_ads_modals(sb, quiet=True):
    """注入广告屏蔽 CSS；quiet=True 时成功不打印日志"""
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
        if not quiet:
            log("已注入广告屏蔽 CSS")
    except Exception as e:
        log(f"注入屏蔽 CSS 失败: {e}", "WARN")


def dismiss_guide_dialog(sb, quiet=True):
    """关闭首次进入时的引导弹窗；quiet=True 时成功不打印日志"""
    selectors = [
        'button[aria-label="Close"]',
        'xpath=//div[contains(@class,"dialog__inner")]//button[@aria-label="Close"]',
        'xpath=//button[.//span[normalize-space()="Skip"]]',
    ]
    for sel in selectors:
        try:
            if sb.is_element_visible(sel):
                sb.click(sel)
                time.sleep(1)
                if not quiet:
                    log(f"已关闭引导弹窗")
                return True
        except Exception:
            continue
    return False


# ====================== 控制台错误检测 ======================
def has_console_connection_error(sb) -> bool:
    try:
        return bool(sb.execute_script('''
            var logs = document.querySelectorAll('#consolelogs .log-line');
            for (var i = 0; i < logs.length; i++) {
                if (logs[i].textContent.includes('Failed to connect to server console')) {
                    return true;
                }
            }
            return false;
        '''))
    except Exception:
        return False


# ====================== Turnstile 处理 ======================
def check_turnstile_solved(sb) -> bool:
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
    log("等待 Turnstile 验证...")
    start = time.time()
    last_click = 0
    last_log = 0
    while time.time() - start < timeout:
        if check_turnstile_solved(sb):
            log("✅ Turnstile 验证成功")
            return True
        now = time.time()
        if now - last_click > 3:
            try:
                sb.uc_gui_click_captcha()
                last_click = now
                if now - last_log > 10:
                    log("尝试点击 Turnstile")
                    last_log = now
            except Exception:
                pass
        time.sleep(1)
    log("⏰ Turnstile 验证超时", "WARN")
    return False


def extract_turnstile_token(sb) -> str:
    try:
        return sb.execute_script('''
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value) return inp.value;
            var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe && iframe.contentWindow) {
                try {
                    var doc = iframe.contentWindow.document;
                    var resp = doc.querySelector('input[name="cf-turnstile-response"]');
                    if (resp && resp.value) return resp.value;
                } catch(e) {}
            }
            return '';
        ''') or ''
    except Exception:
        return ''


def api_verify_captcha(sb, identifier: str, token: str) -> dict:
    script = f'''
        var callback = arguments[arguments.length - 1];
        fetch('/client/api/server/start-captcha/verify', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                serverId: {json.dumps(identifier)},
                cf_turnstile_response: {json.dumps(token)},
                fallback: true
            }})
        }})
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{ callback(d); }})
        .catch(function(e) {{ callback({{ error: e.message }}); }});
    '''
    return sb.execute_async_script(script)


def solve_turnstile_for_api(sb, identifier: str, timeout: int = 90) -> bool:
    start = time.time()
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
        log("CF Turnstile 弹窗未出现，跳过", "WARN")
        return True

    last_click = 0
    solved = False
    while time.time() - start < timeout:
        try:
            if check_turnstile_solved(sb):
                solved = True
                log("✅ Turnstile 已解决")
                break
            now = time.time()
            if now - last_click > 3:
                try:
                    sb.uc_gui_click_captcha()
                    last_click = now
                except Exception:
                    try:
                        sb.execute_script('''
                            var el = document.querySelector(
                                '.wisp-start-captcha-modal .cf-turnstile,' +
                                '.wisp-start-captcha-modal iframe'
                            );
                            if (el) el.click();
                        ''')
                        last_click = now
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(1)

    if not solved:
        log("Turnstile 验证超时", "WARN")
        return False

    token = extract_turnstile_token(sb)
    if not token or len(token) < 20:
        log("未提取到有效 Turnstile token", "WARN")
        return False

    result = api_verify_captcha(sb, identifier, token)
    log(f"验证 API 响应: {result.get('message', str(result))}")
    return True


# ====================== 广告页面处理 ======================
def _get_page_situation(sb) -> str:
    try:
        current_url = sb.get_current_url()
    except Exception:
        return 'unknown'
    if "reward" in current_url or "venatus" in current_url:
        return 'reward'
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
    try:
        alert = sb.driver.switch_to.alert
        alert.accept()
        log("✅ Alert 弹窗已关闭")
        time.sleep(1)
        return True
    except Exception:
        return False


def _handle_adblocker_page(sb) -> bool:
    try:
        sb.execute_script('''
            var btn = document.getElementById('recheck-btn');
            if (btn) btn.click();
        ''')
        log("已点击 'Check again'")
        time.sleep(3)
        return True
    except Exception as e:
        log(f"点击 'Check again' 失败: {e}", "WARN")
        return False


def _wait_for_reward_btn_ready(sb, timeout: int = 90) -> bool:
    log(f"等待广告视频加载就绪（最长 {timeout}s）...")
    start = time.time()
    last_log = 0
    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            result = sb.execute_script('''
                var btn = document.getElementById('embedWatchBtn');
                var panel = document.getElementById('embedPlayBtn');
                if (!btn || !panel) return {ready: false, reason: 'no_element'};
                if (window.getComputedStyle(panel).display === 'none') {
                    return {ready: false, reason: 'panel_hidden'};
                }
                var d = window.getComputedStyle(btn).display;
                var v = window.getComputedStyle(btn).visibility;
                return {ready: d !== 'none' && v !== 'hidden', reason: 'ok'};
            ''')
            if result and result.get('ready'):
                log("✅ 广告已就绪")
                return True
            now = time.time()
            if now - last_log >= 15:
                log(f"广告加载中... [{elapsed}s]")
                last_log = now
        except Exception as e:
            log(f"检查广告就绪状态异常: {e}", "WARN")
        time.sleep(2)
    log(f"⏰ 广告按钮等待超时 ({timeout}s)", "WARN")
    return False


def _click_watch_ad_btn(sb) -> bool:
    methods = [
        lambda: sb.execute_script('''
            var btn = document.getElementById('embedWatchBtn');
            if (!btn) return false;
            btn.click();
            return true;
        '''),
        lambda: (sb.click('#embedWatchBtn') or True),
        lambda: sb.execute_script('''
            var btn = document.getElementById('embedWatchBtn');
            if (!btn) return false;
            btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            return true;
        '''),
    ]
    for i, method in enumerate(methods, 1):
        try:
            if method():
                log(f"✅ 广告按钮点击成功")
                time.sleep(1)
                return True
        except Exception:
            continue
    log("❌ 广告按钮点击失败", "ERROR")
    return False


def _wait_for_ad_completion(sb, identifier: str, timeout: int = 300) -> bool:
    safe_id = mask_server_id(identifier)
    start = time.time()
    console_path = f"/servers/{identifier}/console"
    last_log = 0
    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            current_url = sb.get_current_url()
            if "rewardDone=1" in current_url or console_path in current_url:
                log(f"✅ 广告完成: {safe_id}")
                return True
            try:
                status_info = sb.execute_script('''
                    var st = document.getElementById('embedStatus');
                    if (!st) return {visible: false, text: ''};
                    return {visible: window.getComputedStyle(st).display !== 'none', text: st.textContent || ''};
                ''')
                if status_info and status_info.get('visible'):
                    text = status_info.get('text', '').lower()
                    if any(kw in text for kw in ['starting', 'saving', 'returning', 'session']):
                        log(f"✅ 广告完成: {safe_id}")
                        time.sleep(8)
                        return True
            except Exception:
                pass
            now = time.time()
            if now - last_log >= 30:
                log(f"等待广告完成... [{elapsed}s]")
                last_log = now
        except Exception as e:
            log(f"广告完成检测异常: {e}", "WARN")
        time.sleep(3)
    log(f"⚠️ 广告等待超时 ({timeout}s): {safe_id}", "WARN")
    return False


def handle_reward_ad_flow(sb, identifier: str, console_url: str) -> bool:
    safe_id = mask_server_id(identifier)
    ad_flow_timeout = 15
    start = time.time()
    last_situation = None
    while time.time() - start < ad_flow_timeout:
        if _dismiss_alert_if_present(sb):
            return True
        situation = _get_page_situation(sb)
        if situation != last_situation:
            log(f"当前页面情况: {situation}")
            last_situation = situation
        if situation == 'reward':
            return _execute_reward_ad_watch(sb, identifier)
        elif situation == 'adblocker':
            _handle_adblocker_page(sb)
            time.sleep(3)
            continue
        time.sleep(1)
    if _dismiss_alert_if_present(sb):
        return True
    log("广告流程跳过（无广告）")
    return True


def _execute_reward_ad_watch(sb, identifier: str) -> bool:
    safe_id = mask_server_id(identifier)
    log(f"进入广告观看流程: {safe_id}")
    current_url = sb.get_current_url()

    if "venatus" in current_url:
        log("检测到 Venatus 广告页，等待自动跳转...")
        start = time.time()
        while time.time() - start < 60:
            if sb.get_current_url() != current_url:
                log("✅ Venatus 广告页已跳转")
                break
            time.sleep(1)
        else:
            time.sleep(15)
        return True

    btn_ready = _wait_for_reward_btn_ready(sb, timeout=90)
    if not btn_ready:
        if _dismiss_alert_if_present(sb):
            return True
        if "reward-video" not in sb.get_current_url():
            log("广告页面已自动跳转")
            return True
        log("广告按钮未就绪，继续CF验证", "WARN")
        return True
    if _dismiss_alert_if_present(sb):
        return True
    if not _click_watch_ad_btn(sb):
        return True
    _wait_for_ad_completion(sb, identifier, timeout=300)
    _dismiss_alert_if_present(sb)
    return True


# ====================== 登录流程 ======================
def login(sb, email: str, password: str) -> bool:
    log("访问登录页...")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
    time.sleep(4)
    try:
        sb.wait_for_element_visible('input#email', timeout=15)
        log("✅ 找到登录表单")
    except TimeoutException:
        log("未找到登录表单，重新连接...", "WARN")
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
        time.sleep(5)
        try:
            sb.wait_for_element_visible('input#email', timeout=10)
        except TimeoutException:
            log("仍然未找到登录表单", "ERROR")
            return False
    sb.type('input#email', email)
    time.sleep(0.5)
    sb.type('input#password', password)
    time.sleep(0.5)
    if not wait_for_turnstile_success(sb, timeout=35):
        log("登录 Turnstile 未通过", "ERROR")
        return False
    log("提交登录...")
    try:
        sb.click('button.login-btn')
    except Exception:
        sb.execute_script('document.querySelector("form#login-form").submit()')
    log("等待跳转到仪表盘...")
    for _ in range(15):
        if "/dashboard" in sb.get_current_url() or "/client/dashboard" in sb.get_current_url():
            break
        time.sleep(1)
    else:
        log("登录后未成功跳转到仪表盘", "ERROR")
        return False
    block_ads_modals(sb)
    dismiss_guide_dialog(sb)
    for attempt in range(2):
        dashboard_ready = False
        for sel in ['div.server-list', 'div.servers-container', 'div.card', 'div.server-card', 'table', 'main', 'section', '#app']:
            try:
                sb.wait_for_element_present(sel, timeout=3)
                dashboard_ready = True
                break
            except Exception:
                continue
        if not dashboard_ready:
            try:
                body_len = sb.execute_script("return document.body.innerText.length")
                if body_len and int(body_len) > 100:
                    dashboard_ready = True
            except Exception:
                pass
        if dashboard_ready:
            break
        log("仪表盘未就绪，刷新重试...", "WARN")
        sb.refresh()
        time.sleep(5)
        block_ads_modals(sb)
        dismiss_guide_dialog(sb)
    log("✅ 登录成功")
    return True


# ====================== 获取服务器列表 ======================
def get_servers(sb) -> List[str]:
    for attempt in range(2):
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
                    log(f"获取到 {len(ids)} 台服务器: {[mask_server_id(i) for i in ids]}")
                    return ids
        except Exception as e:
            log(f"fetch 请求失败: {e}", "WARN")
        if attempt == 0:
            time.sleep(10)
    try:
        dom_ids = sb.execute_script('''
            var cards = document.querySelectorAll('[data-server-id], .server-card, .server-item');
            return Array.from(cards).map(function(el) { return el.getAttribute('data-server-id') || el.id; }).filter(Boolean);
        ''')
        if dom_ids:
            log(f"从 DOM 提取到 {len(dom_ids)} 台服务器")
            return list(dom_ids)
    except Exception:
        pass
    log("未能获取任何服务器", "ERROR")
    return []


# ====================== API 辅助函数 ======================
def api_server_action(sb, identifier: str, action: str, retries=2) -> dict:
    script = f'''
        var callback = arguments[arguments.length - 1];
        fetch('/client/api/server/{action}', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ serverId: {json.dumps(identifier)} }})
        }})
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{ callback(d); }})
        .catch(function(e) {{ callback({{ error: e.message }}); }});
    '''
    for attempt in range(retries):
        try:
            result = sb.execute_async_script(script)
            if result and not result.get('error') and 'successfully' in str(result.get('message', '')).lower():
                return result
            elif result and 'An internal server error' in str(result.get('error', '')):
                log(f"API {action} 500 错误，重试 {attempt+1}/{retries}...", "WARN")
                time.sleep(10)
            else:
                return result
        except Exception as e:
            log(f"API {action} 异常 ({attempt+1}/{retries}): {e}", "WARN")
            time.sleep(10)
    return {"error": "API call failed after retries"}


def api_is_installing(sb, identifier: str):
    script = f'''
        var callback = arguments[arguments.length - 1];
        fetch('/client/api/server/isInstalling', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ serverId: {json.dumps(identifier)} }})
        }})
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{ callback(d.isInstalling); }})
        .catch(function() {{ callback(null); }});
    '''
    try:
        return sb.execute_async_script(script)
    except Exception:
        return None


def api_server_status(sb, identifier: str):
    script = f'''
        var callback = arguments[arguments.length - 1];
        fetch('/client/api/servers/status', {{
            method: 'GET',
            headers: {{ 'Accept': 'application/json' }}
        }})
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
            var s = data.servers.find(function(x) {{ return x.identifier === {json.dumps(identifier)}; }});
            callback(s ? s.current_state : null);
        }})
        .catch(function() {{ callback(null); }});
    '''
    try:
        return sb.execute_async_script(script)
    except Exception:
        return None


# ====================== 按钮定位（快速扫描 + 单次等待）======================
def _parse_selector(sel: str):
    if sel.startswith('xpath='):
        return By.XPATH, sel[6:]
    return By.CSS_SELECTOR, sel


def find_power_button(sb, timeout=8):
    """
    快速扫描所有候选（不等待），未命中才进入一次总超时循环。
    避免 8 个候选 × 8 秒 = 64 秒的串行等待。
    """
    candidates = [
        ('xpath=//button[contains(@class,"btn--secondary") and .//span[normalize-space()="Restart"]]', 'Restart'),
        ('xpath=//button[contains(@class,"btn--success") and .//span[normalize-space()="Start"]]', 'Start'),
        ('xpath=//button[.//span[normalize-space()="Restart"]]', 'Restart'),
        ('xpath=//button[.//span[normalize-space()="Start"]]', 'Start'),
        ('button#restart-btn', 'Restart'),
        ('button#start-btn', 'Start'),
    ]

    def scan():
        for sel, name in candidates:
            by, value = _parse_selector(sel)
            try:
                els = sb.driver.find_elements(by, value)
                if els:
                    return els[0], name
            except Exception:
                continue
        return None, None

    btn, name = scan()
    if btn:
        return btn, name
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        btn, name = scan()
        if btn:
            return btn, name
    return None, None


def click_power_button_js(sb):
    """JS 兜底点击；用 IIFE 避免 Illegal return statement"""
    try:
        result = sb.execute_script('''
            (function() {
                var buttons = document.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    if ((buttons[i].textContent || '').trim() === 'Restart') {
                        buttons[i].click();
                        return 'Restart';
                    }
                }
                for (var j = 0; j < buttons.length; j++) {
                    if ((buttons[j].textContent || '').trim() === 'Start') {
                        buttons[j].click();
                        return 'Start';
                    }
                }
                return null;
            })()
        ''')
        if result:
            log(f"✅ 已通过 JS 点击 {result} 按钮")
            return True
    except Exception as e:
        log(f"JS 点击按钮异常: {e}", "WARN")
    return False


def click_start_button(sb) -> bool:
    btn, name = find_power_button(sb, timeout=5)
    if btn:
        try:
            btn.click()
            log(f"备用 UI 点击 {name} 按钮")
            return True
        except Exception as e:
            log(f"备用 UI 点击失败: {e}", "WARN")
    return click_power_button_js(sb)


def wait_for_server_running(sb, identifier: str, timeout: int = 180) -> bool:
    """轮询状态；仅在状态变化或每 30 秒打印一次"""
    safe_id = mask_server_id(identifier)
    log(f"等待服务器 running（最长 {timeout}s）: {safe_id}")
    start = time.time()
    last_status = None
    last_log = 0
    while time.time() - start < timeout:
        try:
            status = api_server_status(sb, identifier)
            if status and 'running' in str(status).lower():
                log(f"✅ 服务器 {safe_id} 状态: {status}")
                return True
            now = time.time()
            if status != last_status or now - last_log >= 30:
                log(f"当前状态: {status or '未知'}，继续等待...")
                last_status = status
                last_log = now
        except Exception as e:
            log(f"状态检查异常: {e}", "WARN")
        time.sleep(5)
    log(f"❌ 等待超时", "ERROR")
    return False


# ====================== 重启服务器 ======================
def restart_server(sb, identifier: str) -> bool:
    console_url = CONSOLE_URL_TEMPLATE.format(identifier=identifier)
    safe_id = mask_server_id(identifier)
    log(f"{'─'*40}")
    log(f"重启服务器: {safe_id}")
    log(f"{'─'*40}")

    # Step 1: 导航
    sb.get(console_url)
    time.sleep(5)
    block_ads_modals(sb)
    dismiss_guide_dialog(sb)

    # Step 2: 定位并点击
    start_btn, btn_name = find_power_button(sb, timeout=8)
    ui_clicked = False
    if start_btn:
        log(f"找到 {btn_name} 按钮")
        try:
            start_btn.click()
            ui_clicked = True
            log(f"✅ 已点击 {btn_name}，触发广告/启动流程")
        except Exception:
            ui_clicked = click_power_button_js(sb)
    else:
        log("未找到 Start/Restart 按钮（UI），尝试 JS 兜底", "WARN")
        ui_clicked = click_power_button_js(sb)

    if not ui_clicked:
        log("UI 点击失败，直接调用 API restart 保底...", "WARN")
        result = api_server_action(sb, identifier, "restart", retries=2)
        msg = str(result.get("message", result))
        if not result.get("error") and 'successfully' in msg.lower():
            log(f"✅ API restart 成功")
            if wait_for_server_running(sb, identifier, timeout=180):
                return True
        else:
            log(f"API restart 未成功: {msg}", "WARN")
        sb.get(console_url)
        time.sleep(3)
        block_ads_modals(sb)
        if click_start_button(sb):
            time.sleep(5)
            if wait_for_server_running(sb, identifier, timeout=180):
                return True
        log(f"❌ 服务器 {safe_id} 启动失败", "ERROR")
        return False

    time.sleep(3)

    # Step 3: 广告
    handle_reward_ad_flow(sb, identifier, console_url)

    # Step 4: 回到控制台
    sb.get(console_url)
    time.sleep(3)
    block_ads_modals(sb)
    dismiss_guide_dialog(sb)

    # Step 5: Turnstile
    solve_turnstile_for_api(sb, identifier, timeout=90)

    # Step 6: 安装等待
    log(f"等待安装完成（最多 60s）: {safe_id}")
    install_start = time.time()
    while time.time() - install_start < 60:
        installing = api_is_installing(sb, identifier)
        if installing is False:
            log("✅ 安装完成")
            break
        time.sleep(5)
    else:
        log("安装等待超时", "WARN")

    # Step 7: API 启动
    api_success = False
    for attempt in range(2):
        for action in ("restart", "start"):
            result = api_server_action(sb, identifier, action, retries=2)
            msg = result.get("message", str(result))
            if not result.get("error") and 'successfully' in str(msg).lower():
                log(f"API {action} 成功")
                api_success = True
                break
        if api_success:
            break
        time.sleep(10)

    # Step 8: 轮询
    if api_success:
        if wait_for_server_running(sb, identifier, timeout=180):
            return True

    # Step 9: UI 保底
    log("启动保底 UI 方案...")
    sb.get(console_url)
    time.sleep(3)
    block_ads_modals(sb)
    dismiss_guide_dialog(sb)
    if click_start_button(sb):
        time.sleep(5)
        if wait_for_server_running(sb, identifier, timeout=180):
            return True

    log(f"❌ 服务器 {safe_id} 启动失败", "ERROR")
    return False


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
                              f"❌ 登录失败\n账号: {email}\n\nWispbyte Auto Restart")
                return
            servers = get_servers(sb)
            if not servers:
                screenshot = take_screenshot(sb, idx, "no-server")
                send_tg_photo(tg_token, tg_chat, screenshot,
                              f"❌ 未找到服务器\n账号: {email}\n\nWispbyte Auto Restart")
                return
            for si, server_id in enumerate(servers, start=1):
                console_url = CONSOLE_URL_TEMPLATE.format(identifier=server_id)
                sb.get(console_url)
                time.sleep(5)
                block_ads_modals(sb)
                dismiss_guide_dialog(sb)
                if has_console_connection_error(sb):
                    log(f"❌ 控制台连接错误，跳过: {mask_server_id(server_id)}")
                    screenshot = take_screenshot(sb, idx, f"console-error-{si}")
                    send_tg_photo(tg_token, tg_chat, screenshot,
                                  f"⚠️ 控制台连接错误，跳过重启\n\n账号: {email}\n服务器: {server_id}\n\nWispbyte Auto Restart")
                    continue
                success = restart_server(sb, server_id)
                suffix = f"done-{si}" if len(servers) > 1 else "done"
                screenshot = take_screenshot(sb, idx, suffix)
                status_icon = "✅" if success else "❌"
                status_text = "重启成功" if success else "重启失败"
                caption = (
                    f"{status_icon} {status_text}\n\n"
                    f"账号: {email}\n"
                    f"服务器: {server_id}\n\n"
                    f"Wispbyte Auto Restart"
                )
                send_tg_photo(tg_token, tg_chat, screenshot, caption)
        except Exception as e:
            log(f"账号 {idx} 处理异常: {e}", "ERROR")
            screenshot = take_screenshot(sb, idx, "exception")
            send_tg_photo(tg_token, tg_chat, screenshot,
                          f"❌ 脚本异常\n账号: {email}\n信息: {str(e)[:200]}\n\nWispbyte Auto Restart")


# ====================== 账号加载 & 入口 ======================
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
                log(f"WISPBYTE_{i} 格式不正确", "WARN")
        else:
            log(f"WISPBYTE_{i} 格式错误", "WARN")
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
            log(f"无效邮箱: '{mask_email(email)}'", "WARN")
            continue
        if email in seen:
            log(f"重复邮箱: '{mask_email(email)}'", "WARN")
            continue
        seen.add(email)
        result.append(email)
    return result


def main():
    tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not tg_token or not tg_chat:
        log("缺少 TG_BOT_TOKEN 或 TG_CHAT_ID", "WARN")
    all_accounts = load_accounts()
    if not all_accounts:
        log("未找到任何有效账号", "ERROR")
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
                log(f"邮箱 '{mask_email(target)}' 未在已配置账号中找到", "WARN")
        if not selected:
            log("指定的邮箱全部无效", "ERROR")
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
