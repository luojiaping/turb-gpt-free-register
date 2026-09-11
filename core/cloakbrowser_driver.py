# -*- coding: utf-8 -*-
"""CloakBrowser 的 Selenium 风格轻量适配层。"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from config import cloakbrowser as _cfg

logger = logging.getLogger(__name__)

# cloakbrowser.launch() 内含 sync_playwright().start()、node 子进程派生、
# human 补丁全局标记、wrapper 更新检查等进程级共享状态，并发 launch 会互相
# 干扰（曾导致 Playwright 报 "Sync API inside the asyncio loop" 并毒化线程）。
# 这里把整个 launch 过程串行化；任务主体（页面操作）仍保持并发。
_LAUNCH_LOCK = threading.Lock()

# Selenium Keys 私有区字符 -> Playwright 按键名（None 表示无法映射，忽略）。
_SELENIUM_KEYS = {
    "\ue000": None,   # NULL
    "\ue001": None,   # CANCEL
    "\ue002": None,   # HELP
    "\ue003": "Backspace",
    "\ue004": "Tab",
    "\ue005": None,   # CLEAR
    "\ue006": "Enter",  # RETURN
    "\ue007": "Enter",  # ENTER
    "\ue008": "Shift",
    "\ue009": "Control",
    "\ue00a": "Alt",
    "\ue00b": None,   # PAUSE
    "\ue00c": "Escape",
    "\ue00d": "Space",
    "\ue00e": "PageUp",
    "\ue00f": "PageDown",
    "\ue010": "End",
    "\ue011": "Home",
    "\ue012": "ArrowLeft",
    "\ue013": "ArrowUp",
    "\ue014": "ArrowRight",
    "\ue015": "ArrowDown",
    "\ue016": "Insert",
    "\ue017": "Delete",
    "\ue03d": "Meta",  # COMMAND
}


def _platform_mod() -> str:
    import sys
    return "Meta" if sys.platform == "darwin" else "Control"


@dataclass
class CloakOpenResult:
    profile_id: str = "cloakbrowser"
    raw: dict | None = None


class CloakElement:
    def __init__(self, page, locator=None, handle=None):
        self.page = page
        self.locator = locator
        self.handle = handle

    def _handle(self):
        if self.handle is not None:
            return self.handle
        return self.locator.element_handle(timeout=5000)

    def _eval(self, expression: str, arg: Any = None) -> Any:
        if self.locator is not None:
            try:
                return self.locator.evaluate(expression, arg, timeout=3000)
            except TypeError:
                return self.locator.evaluate(expression, arg)
        return self.handle.evaluate(expression, arg)

    def _eval_handle(self, expression: str, arg: Any = None) -> Any:
        h = self._handle()
        return h.evaluate_handle(expression, arg)

    def is_displayed(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_visible(timeout=800))
            return bool(self.handle.evaluate("el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_enabled(timeout=800))
            return bool(self.handle.evaluate("el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"))
        except Exception:
            return False

    def click(self) -> None:
        if self.locator is not None:
            self.locator.click(timeout=10000)
        else:
            self.handle.click(timeout=10000)

    def clear(self) -> None:
        try:
            if self.locator is not None:
                self.locator.fill("", timeout=10000)
            else:
                self.handle.fill("", timeout=10000)
        except Exception:
            # 部分非 input 元素不支持 fill，回退键盘清空。
            self.click()
            self.page.keyboard.press(f"{_platform_mod()}+A")
            self.page.keyboard.press("Backspace")

    @property
    def tag_name(self) -> str:
        try:
            return str(self._eval("el => el.tagName.toLowerCase()") or "")
        except Exception:
            return ""

    @property
    def text(self) -> str:
        """兼容 Selenium WebElement.text：返回元素可见文本。"""
        try:
            return str(self._eval("el => el.innerText || el.textContent || ''") or "")
        except Exception:
            return ""

    def _is_focused(self) -> bool:
        """元素已是 document.activeElement 时返回 True，避免重复点击导致光标跳动。"""
        try:
            return bool(self._eval("el => document.activeElement === el"))
        except Exception:
            return False

    def send_keys(self, *values: str) -> None:
        # 兼容 Selenium: el.send_keys(Keys.COMMAND, 'a') / 逐字符追加。
        # 注意：Selenium 的 send_keys 不会点击元素；只有在元素尚未聚焦时才点击，
        # 否则每次逐字符输入前的中心点击会把光标跳到文本中间，导致字符乱序。
        text = "".join(str(v or "") for v in values)
        if not self._is_focused():
            try:
                self.click()
            except Exception:
                pass
        if not text:
            return
        try:
            self._type_keys(text)
            return
        except Exception:
            pass
        # 回退：无特殊键时用 fill 一次性设置；含特殊键时逐个降级为按键。
        if any(ch in _SELENIUM_KEYS for ch in text):
            try:
                self.page.keyboard.press(text.replace("\ue009", "Control").replace("\ue03d", "Meta"))
            except Exception:
                pass
            return
        try:
            if self.locator is not None:
                self.locator.fill(text, timeout=10000)
            else:
                self.handle.fill(text, timeout=10000)
        except Exception:
            self.page.keyboard.type(text, delay=35)

    def _type_keys(self, text: str) -> None:
        """按 Selenium Keys 语义发送按键：普通文本追加输入，特殊键映射为按键。"""
        kb = self.page.keyboard
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch in _SELENIUM_KEYS:
                key = _SELENIUM_KEYS[ch]
                if key is None:
                    # 无法映射的私有区字符，跳过（保持 Selenium 对 NULL 等无操作的语义）
                    i += 1
                    continue
                if key in ("Control", "Meta"):
                    # 组合键：mod + 紧随其后的普通字符，如 Ctrl+A / Cmd+A
                    mod = "Control" if key == "Control" else "Meta"
                    if i + 1 < n and text[i + 1] not in _SELENIUM_KEYS:
                        kb.press(f"{mod}+{text[i + 1].upper()}")
                        i += 2
                        continue
                    i += 1
                    continue
                kb.press(key)
                i += 1
                continue
            # 普通文本段：累积后一次性输入，模拟真实打字节奏
            j = i
            buf = []
            while j < n and text[j] not in _SELENIUM_KEYS:
                buf.append(text[j])
                j += 1
            kb.type("".join(buf), delay=35)
            i = j

    def get_attribute(self, name: str) -> str | None:
        try:
            if self.locator is not None:
                return self.locator.get_attribute(name, timeout=1000)
            return self.handle.get_attribute(name)
        except Exception:
            return None


class _SwitchTo:
    def __init__(self, driver: "CloakSeleniumDriver"):
        self._driver = driver

    def window(self, handle: str) -> None:
        self._driver._switch_window(handle)


class CloakSeleniumDriver:
    """只实现本项目 Roxy Selenium 流程实际用到的 WebDriver 子集。"""

    def __init__(self, browser: Any, context: Any | None, page: Any):
        self.browser = browser
        self.context = context
        self.page = page
        self._page_load_timeout_ms = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 1000
        self.switch_to = _SwitchTo(self)

    @property
    def current_url(self) -> str:
        return str(getattr(self.page, "url", "") or "")

    @property
    def window_handles(self) -> list[str]:
        pages = self._pages()
        return [str(i) for i in range(len(pages))]

    def _pages(self) -> list[Any]:
        try:
            if self.context is not None:
                return list(self.context.pages)
        except Exception:
            pass
        try:
            contexts = list(getattr(self.browser, "contexts", []) or [])
            pages = []
            for ctx in contexts:
                pages.extend(list(getattr(ctx, "pages", []) or []))
            return pages or [self.page]
        except Exception:
            return [self.page]

    def _switch_window(self, handle: str) -> None:
        pages = self._pages()
        idx = int(handle)
        self.page = pages[idx]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def set_page_load_timeout(self, seconds: int) -> None:
        self._page_load_timeout_ms = int(seconds) * 1000
        try:
            self.page.set_default_navigation_timeout(self._page_load_timeout_ms)
            self.page.set_default_timeout(self._page_load_timeout_ms)
        except Exception:
            pass

    def get(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def quit(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass

    def find_elements(self, by: Any, selector: str) -> list[CloakElement]:
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [CloakElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> CloakElement:
        els = self.find_elements(by, selector)
        if not els:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return els[0]

    def _locator(self, by: Any, selector: str):
        by_s = str(by or "").lower()
        if "xpath" in by_s or str(selector).startswith("//"):
            return self.page.locator(f"xpath={selector}")
        return self.page.locator(selector)

    def execute_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=False)

    def execute_async_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=True)

    def execute_cdp_cmd(self, cmd: str, params: dict | None = None) -> Any:
        params = params or {}
        client = None
        try:
            client = self.context.new_cdp_session(self.page) if self.context is not None else self.page.context.new_cdp_session(self.page)
            return client.send(cmd, params)
        except Exception as exc:
            logger.debug("[Cloak] CDP 命令失败 %s: %s", cmd, exc)
            return None
        finally:
            # CDP session 必须 detach，否则每次调用泄漏一个 session，
            # 累积会耗尽浏览器资源导致传输层冻结。
            if client is not None:
                try:
                    client.detach()
                except Exception:
                    pass

    def _serialize_args(self, args: tuple[Any, ...]) -> tuple[CloakElement | None, list[Any]]:
        """拆分 Selenium 脚本参数。

        Playwright 的 JSHandle/ElementHandle 不能可靠地嵌在 dict/list payload 中跨
        page.evaluate 传递；Selenium 脚本最常见模式是 `arguments[0]` 为元素，
        因此这里把第一个 CloakElement 作为真实 DOM `el` 传入，其它参数保持
        JSON 可序列化。
        """
        first_el = args[0] if args and isinstance(args[0], CloakElement) else None
        rest = list(args[1:] if first_el else args)
        cleaned = []
        for item in rest:
            if isinstance(item, CloakElement):
                # 极少数脚本会传多个元素；用真实 handle 直接会在嵌套 payload 中失效，
                # 这里退化为 None，比把错误对象传进 JS 更安全。
                cleaned.append(None)
            else:
                cleaned.append(item)
        return first_el, cleaned

    @staticmethod
    def _unwrap_js_result(page, handle: Any) -> Any:
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return CloakElement(page, handle=element)
        try:
            value = handle.json_value()
        except Exception as exc:
            msg = str(exc)
            if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                logger.info("[Cloak] JS 执行后页面发生跳转，忽略返回值读取失败：%s", msg[:160])
                return {"ok": True, "reason": "navigation_after_script"}
            raise
        try:
            # 脚本返回 {input, button, target...} 这类嵌套 DOM 元素的 dict 时，
            # json_value 会把元素压成 'ref: <Node>' 字符串（旧版本为 {}）。
            # 逐 key 用 get_property 恢复为 CloakElement。
            if isinstance(value, dict):
                for dict_key in list(value.keys()):
                    existing = value.get(dict_key)
                    if existing is not None and existing != {} and not (
                        isinstance(existing, str) and existing.startswith("ref:")
                    ):
                        continue
                    try:
                        prop = handle.get_property(str(dict_key))
                    except Exception:
                        continue
                    try:
                        prop_el = prop.as_element()
                    except Exception:
                        prop_el = None
                    if prop_el is not None:
                        # as_element 返回同一底层 handle，绝不能 dispose，
                        # 否则刚恢复的元素立即失效。
                        value[dict_key] = CloakElement(page, handle=prop_el)
                    else:
                        try:
                            prop.dispose()
                        except Exception:
                            pass
            return value
        except Exception:
            return value
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _evaluate(self, script: str, args: tuple[Any, ...], async_mode: bool) -> Any:
        first_el, serial_args = self._serialize_args(args)
        if async_mode:
            wrapper = """async ({script, args}) => {
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            element_wrapper = """async (el, payload) => {
              const args = [el, ...payload.args];
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', payload.script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            if first_el is not None:
                result = first_el._eval(element_wrapper, {"script": script, "args": serial_args})
            else:
                try:
                    result = self.page.evaluate(wrapper, {"script": script, "args": serial_args})
                except Exception as exc:
                    if _is_navigation_destroyed_error(exc):
                        logger.debug("[Cloak] 异步脚本执行中页面导航，返回空由调用侧重试")
                        return None
                    raise
            if isinstance(result, dict) and result.get("__cloak_timeout"):
                raise TimeoutError("execute_async_script timeout")
            return result

        # Selenium 脚本经常以 `return ...` 为主体；用 Function 保持语义。
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        element_wrapper = """(el, payload) => {
          const args = [el, ...payload.args];
          const fn = new Function(...args.map((_, i) => 'a' + i), payload.script);
          return fn(...args);
        }"""
        if first_el is not None:
            handle = first_el._eval_handle(element_wrapper, {"script": script, "args": serial_args})
        else:
            try:
                handle = self.page.evaluate_handle(wrapper, {"script": script, "args": serial_args})
            except Exception as exc:
                if _is_navigation_destroyed_error(exc):
                    # 调用瞬间页面发生导航：返回空 dict（falsy），调用侧轮询会重试。
                    logger.debug("[Cloak] 同步脚本执行中页面导航，返回空由调用侧重试")
                    return {}
                raise
        return self._unwrap_js_result(self.page, handle)


def _is_navigation_destroyed_error(exc: BaseException) -> bool:
    """页面导航导致执行上下文销毁（调用侧轮询重试即可，不应直接判失败）。

    注意：超时（TimeoutError）不在此列，必须继续向上传播。
    """
    msg = str(exc).lower()
    return any(
        fragment in msg
        for fragment in (
            "execution context was destroyed",
            "navigation",
            "target crashed",
            "target closed",
            "session closed",
        )
    )


def _normalize_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    return proxy.replace("socks5h://", "socks5://")


def _detect_cloak_exit_geo(proxy_url: str | None = None) -> dict:
    """按当前/代理出口检测地理信息，供 Cloak 显式 locale/timezone 使用。"""
    try:
        import requests
        from config import browser as _browser_cfg
        endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        timeout = float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
    except Exception:
        return {}
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            timezone = data.get("timezone")
            if isinstance(timezone, dict):
                timezone = timezone.get("id") or timezone.get("name")
            geo = {
                "ip": data.get("ip") or data.get("query"),
                "country": (data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper(),
                "region": data.get("region") or data.get("regionName"),
                "city": data.get("city"),
                "timezone": timezone or "",
                "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
            }
            if geo.get("country") or geo.get("timezone"):
                logger.info(
                    "[Cloak] 出口IP地理信息：ip=%s country=%s city=%s timezone=%s",
                    geo.get("ip") or "?", geo.get("country") or "?", geo.get("city") or "?", geo.get("timezone") or "?",
                )
                return geo
        except Exception as exc:
            logger.debug("[Cloak] 出口 IP 地理检测失败 endpoint=%s: %s: %s", url, type(exc).__name__, exc)
    return {}


def _build_cloak_locale_options(proxy_url: str | None = None) -> dict:
    """生成 Cloak/Playwright 双层语言时区配置。"""
    explicit_locale = str(getattr(_cfg, "CLOAK_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "CLOAK_TIMEZONE", "") or "").strip()
    out = {}
    if explicit_locale:
        out["locale"] = explicit_locale
        # Accept-Language 用 config.browser 自动推断更完整；显式时给一个保守值。
        out["accept_language"] = f"{explicit_locale},{explicit_locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    if explicit_locale and explicit_timezone:
        return out
    if not bool(getattr(_cfg, "CLOAK_GEOIP", True)):
        return out
    try:
        from config.browser import build_browser_environment
        geo = _detect_cloak_exit_geo(proxy_url)
        profile = build_browser_environment(geo)
        out.setdefault("locale", str(profile.get("navigator_language") or ""))
        out.setdefault("timezone", str(profile.get("timezone_iana") or ""))
        out.setdefault("accept_language", str(profile.get("accept_language") or ""))
        out["geo"] = geo
    except Exception as exc:
        logger.debug("[Cloak] 构建自动语言/时区失败：%s: %s", type(exc).__name__, exc)
    return {k: v for k, v in out.items() if v}


def _cloak_chrome_processes() -> dict[int, str]:
    """当前所有 Cloak Chromium 进程 {pid: commandline}（按可执行路径识别）。

    用 wmic 一次取回路径与命令行；wmic 不可用时保守返回空 dict，不误杀。
    """
    import csv as _csv
    import io as _io
    import subprocess as _sp

    try:
        out = _sp.run(
            ["wmic", "process", "where", "name='chrome.exe'",
             "get", "ProcessId,ExecutablePath,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=25,
        ).stdout
    except Exception:
        return {}
    result: dict[int, str] = {}
    try:
        for row in _csv.reader(_io.StringIO(out or "")):
            if len(row) < 4:
                continue
            if ".cloakbrowser" not in str(row[1] or "").lower():
                continue
            try:
                pid = int(str(row[3]).strip())
            except ValueError:
                continue
            result[pid] = str(row[2] or "")
    except Exception:
        return {}
    return result


def _cloak_chrome_pids() -> set[int]:
    """当前所有 Cloak Chromium 进程 PID（按可执行路径识别）。"""
    try:
        return set(_cloak_chrome_processes().keys())
    except Exception:
        return set()


def _extract_user_data_dir(cmdline: str) -> str:
    """从 chrome 命令行提取 --user-data-dir（每次 launch 唯一，用于精确归因）。"""
    try:
        import re as _re
        match = _re.search(r"--user-data-dir[=\s]+(\"[^\"]+\"|\S+)", str(cmdline or ""))
        if not match:
            return ""
        return match.group(1).strip().strip('"').rstrip("\\/")
    except Exception:
        return ""


def kill_cloak_browser_pids(pids: set[int] | list[int] | None) -> None:
    """兜底关闭 Cloak 浏览器进程树（quit 超时且传输层冻结时使用）。"""
    targets = [int(p) for p in (pids or []) if int(p or 0) > 0]
    if not targets:
        return
    import subprocess

    for pid in targets:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=15,
            )
            logger.info("[Cloak] 已兜底关闭浏览器进程树 pid=%s", pid)
        except Exception as exc:
            logger.debug("[Cloak] 兜底关闭浏览器失败 pid=%s: %s", pid, exc)


def reap_cloak_browser(driver: Any) -> None:
    """收尾回收：quit 后仍存活的本次浏览器进程按路径+user-data-dir 确认后强杀。

    匹配规则（任一满足即杀，均为本次 launch 专属标识，不会误伤用户 Chrome
    或其他并发任务的浏览器）：
    1. 本次记录 PID ∩ 当前存活的 cloakbrowser 路径进程；
    2. 命令行 --user-data-dir 落在本次记录的 data-dir 集合内。
    """
    try:
        recorded = {int(p) for p in (getattr(driver, "_cloak_pids", None) or []) if int(p or 0) > 0}
        data_dirs = {str(d) for d in (getattr(driver, "_cloak_data_dirs", None) or []) if str(d or "").strip()}
    except Exception:
        return
    if not recorded and not data_dirs:
        return
    try:
        alive = _cloak_chrome_processes()
    except Exception:
        return
    targets = sorted(recorded & set(alive.keys()))
    if data_dirs:
        for pid, cmdline in alive.items():
            if pid in targets:
                continue
            udd = _extract_user_data_dir(cmdline)
            if udd and udd in data_dirs:
                targets.append(pid)
        targets.sort()
    if not targets:
        return
    logger.info("[Cloak] quit 后仍有 %s 个浏览器进程存活，回收：%s", len(targets), targets)
    kill_cloak_browser_pids(targets)


def build_cloak_driver(proxy: str | None = None) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """启动 CloakBrowser 并返回 Selenium 风格 driver。

    proxy=None  时按 config.proxy.PROXY_POOL 随机抽取；
    proxy=""    时显式禁用代理；
    proxy="..." 时使用指定代理。

    注意：整个 launch 过程持有进程级串行锁（见 _LAUNCH_LOCK），调用方无需额外同步。
    """
    import os

    # 关掉 cloakbrowser 每次 launch 的 wrapper 更新检查（进程级全局标记+网络请求，
    # 并发 launch 下天然竞态）。用户如需更新，手动升级 cloakbrowser 包即可。
    os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")
    with _LAUNCH_LOCK:
        return _build_cloak_driver_locked(proxy=proxy)


def _build_cloak_driver_locked(proxy: str | None = None) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """build_cloak_driver 的实际实现（调用时必须已持有 _LAUNCH_LOCK）。"""
    if proxy is None and bool(getattr(_cfg, "CLOAK_USE_PROXY", True)):
        try:
            from config.proxy import pick_proxy
            proxy = pick_proxy()
        except Exception:
            proxy = None
    try:
        from cloakbrowser import launch, launch_persistent_context
    except ImportError as exc:
        raise RuntimeError("未安装 cloakbrowser，请执行：pip install cloakbrowser") from exc

    launch_args = list(getattr(_cfg, "CLOAK_EXTRA_ARGS", []) or [])
    seed = str(getattr(_cfg, "CLOAK_FINGERPRINT_SEED", "") or "").strip()
    if seed:
        launch_args.append(f"--fingerprint={seed}")

    proxy_url = _normalize_proxy(proxy) if bool(getattr(_cfg, "CLOAK_USE_PROXY", True)) else None
    locale_opts = _build_cloak_locale_options(proxy_url)
    # geoip=True 交给 CloakBrowser 根据当前出口 IP 自动匹配 timezone/locale/WebRTC。
    # 之前只有显式 proxy_url 时才开启；如果用户走系统代理/VPN/透明代理，代码层面
    # 看不到 proxy_url，会误关 geoip，导致语言/时区不跟随出口。这里改为完全尊重配置。
    opts = {
        "headless": bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
        "humanize": bool(getattr(_cfg, "CLOAK_HUMANIZE", True)),
        "geoip": bool(getattr(_cfg, "CLOAK_GEOIP", True)),
    }
    if locale_opts.get("locale"):
        opts["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        opts["timezone"] = locale_opts["timezone"]
    if proxy_url:
        opts["proxy"] = proxy_url
    if launch_args:
        opts["args"] = launch_args
    license_key = str(getattr(_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
    if license_key:
        opts["license_key"] = license_key

    user_data_dir = str(getattr(_cfg, "CLOAK_USER_DATA_DIR", "") or "").strip()
    logger.info(
        "[Cloak] 启动 CloakBrowser：headless=%s humanize=%s geoip=%s proxy=%s locale=%s timezone=%s accept_language=%s persistent=%s",
        opts.get("headless"), opts.get("humanize"), opts.get("geoip"),
        proxy_url or "无", opts.get("locale") or "自动/默认", opts.get("timezone") or "自动/默认",
        locale_opts.get("accept_language") or "自动/默认", bool(user_data_dir),
    )
    context_kwargs = {}
    if locale_opts.get("locale"):
        context_kwargs["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        context_kwargs["timezone_id"] = locale_opts["timezone"]
    if locale_opts.get("accept_language"):
        context_kwargs["extra_http_headers"] = {"Accept-Language": locale_opts["accept_language"]}

    before_procs = _cloak_chrome_processes()
    if user_data_dir:
        context = launch_persistent_context(user_data_dir, **opts)
        page = context.new_page()
        browser = getattr(context, "browser", None) or context
        # persistent context 的 locale/timezone 已通过 launch_persistent_context 参数传入。
    else:
        browser = launch(**opts)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

    driver = CloakSeleniumDriver(browser=browser, context=context, page=page)
    # 记录本次启动新增的浏览器 PID 及其 user-data-dir，供收尾精确回收。
    # user-data-dir 每次 launch 唯一，即使并发 launch 交错也不会张冠李戴。
    try:
        after_procs = _cloak_chrome_processes()
        new_pids = sorted(set(after_procs) - set(before_procs))
        driver._cloak_pids = new_pids
        driver._cloak_data_dirs = sorted({
            _extract_user_data_dir(after_procs[pid])
            for pid in new_pids
            if _extract_user_data_dir(after_procs.get(pid, ""))
        })
    except Exception:
        driver._cloak_pids = []
        driver._cloak_data_dirs = []
    # Roxy/Cloak 共用部分页面操作函数；给共享函数一个显式日志前缀，
    # 避免 Cloak 注册流程里出现 `[Roxy注册]`。
    driver._registration_log_prefix = "[Cloak注册]"
    driver.set_page_load_timeout(int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
    return driver, CloakOpenResult(raw={"driver": "cloakbrowser", "proxy": proxy_url, "locale": locale_opts, "options": {k: v for k, v in opts.items() if k != "license_key"}})
