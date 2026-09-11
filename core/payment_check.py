# -*- coding: utf-8 -*-
"""支付类型检测引擎。

按区域线路（国家|币种|语言|代理）创建一个带 plus-1-month-free 活动的
ChatGPT Plus Custom Checkout，再从 OAICS Checkout 状态或 Stripe Payment Page
初始化响应中读取当前线路返回的支付方式。

边界：只建单并读取状态，不确认付款、不提交支付信息、不绑定支付方式。
每条线路通常创建一个独立 Checkout，会增加远端建单次数并可能触发 429 或风控。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from config import OAI_CLIENT_BUILD_NUMBER, OAI_CLIENT_VERSION
from config import payment as cfg
from core.chatgpt_plan import token_claims
from core.openai_auth import build_sentinel_header, request_sentinel_token
from core.session import BrowserSession

logger = logging.getLogger(__name__)

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_SESSION_ID_RE = re.compile(r"(oaics_[A-Za-z0-9]+|cs_(?:live|test)_[A-Za-z0-9]+)")
_STRIPE_PK_RE = re.compile(r"pk_(?:live|test)_[A-Za-z0-9]+")
_LOC_RE = re.compile(r"^loc=([A-Za-z]{2})\s*$", re.MULTILINE)
_JWT_RE = re.compile(r"[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_PROXY_SCHEMES = ("http://", "https://", "socks5://", "socks5h://")

_PAYMENT_FIELD_KEYS = (
    "payment_method_types",
    "ordered_payment_method_types",
    "payment_method_specs",
    "custom_payment_methods",
)

# 视为“正常完成”的线路 state；代理错误、风控、限流、Token 错误和建单错误都不算。
COMPLETED_STATES = frozenset({"payment_methods_available", "payment_methods_empty"})

# 操作日志视为成功完成的总体状态（不等于支付成功）。
SUCCESS_STATUSES = frozenset({"available", "partial", "not_returned", "already_paid"})

_METHOD_ALIASES = {
    "card_payment": "card",
    "direct_card": "card",
    "kakao": "kakao_pay",
    "go_pay": "gopay",
    "grab_pay": "grabpay",
}

_RATE_LOCK = None
_NEXT_ROUTE_AT = 0.0


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _get_rate_lock():
    global _RATE_LOCK
    if _RATE_LOCK is None:
        import threading

        _RATE_LOCK = threading.Lock()
    return _RATE_LOCK


def _wait_route_slot() -> None:
    """线路启动限速：全局最小间隔 + 随机抖动（只限制线路启动，不限制线路内请求）。"""
    global _NEXT_ROUTE_AT
    try:
        min_interval = max(0, int(float(getattr(cfg, "PAYMENT_CHECK_MIN_INTERVAL_MS", 3000) or 0))) / 1000.0
        jitter = max(0, int(float(getattr(cfg, "PAYMENT_CHECK_JITTER_MS", 500) or 0))) / 1000.0
    except (TypeError, ValueError):
        min_interval, jitter = 3.0, 0.5
    lock = _get_rate_lock()
    with lock:
        now = time.monotonic()
        scheduled = max(now, _NEXT_ROUTE_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_ROUTE_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


# ============================================================
# 区域线路解析
# ============================================================

def normalize_route_proxy(value: Any) -> str | None:
    """规范化线路代理；DIRECT/空值/无法识别返回 None（禁用线路，不回退直连）。"""
    raw = str(value or "").strip().strip('"').strip("'")
    if not raw or raw.upper() == "DIRECT":
        return None
    if raw.lower().startswith(_PROXY_SCHEMES):
        return raw
    if "@" in raw:
        return f"http://{raw}"
    parts = [p.strip() for p in raw.split(":")]
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4 and parts[1].isdigit():
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    return None


def parse_payment_routes(route_lines: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """解析 PAYMENT_PROXY_ROUTES，返回 (routes, errors)。

    routes 元素：{"country", "currency", "locale", "proxies"}；
    相同 国家+币种+语言 的多行合并成一个代理池（保持首次出现顺序）。
    """
    raw_lines = list(route_lines if route_lines is not None else (getattr(cfg, "PAYMENT_PROXY_ROUTES", []) or []))
    try:
        max_routes = max(1, int(getattr(cfg, "PAYMENT_CHECK_MAX_ROUTES", 12) or 12))
    except (TypeError, ValueError):
        max_routes = 12

    routes: list[dict] = []
    indexes: dict[str, int] = {}
    errors: list[str] = []
    for line_no, line in enumerate(raw_lines, 1):
        text = str(line or "").strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split("|", 3)
        if len(parts) != 4:
            errors.append(f"第{line_no}行格式错误（应为 国家|币种|语言|代理）")
            continue
        country, currency, locale, proxy_raw = (p.strip() for p in parts)
        country = country.upper()
        currency = currency.upper()
        if not _COUNTRY_RE.match(country):
            errors.append(f"第{line_no}行国家代码无效：{country or '(空)'}（应为两位大写字母）")
            continue
        if not _CURRENCY_RE.match(currency):
            errors.append(f"第{line_no}行币种无效：{currency or '(空)'}（应为三位大写字母）")
            continue
        if not locale:
            errors.append(f"第{line_no}行语言为空")
            continue
        proxy = normalize_route_proxy(proxy_raw)
        if not proxy:
            errors.append(f"第{line_no}行代理无效或为 DIRECT，该线路已禁用")
            continue
        key = f"{country}|{currency}|{locale.lower()}"
        if key not in indexes:
            if len(routes) >= max_routes:
                errors.append(f"支付检测线路最多支持 {max_routes} 组")
                break
            indexes[key] = len(routes)
            routes.append({"country": country, "currency": currency, "locale": locale, "proxies": []})
        route = routes[indexes[key]]
        if proxy not in route["proxies"]:
            route["proxies"].append(proxy)
    return routes, errors


def pick_route_proxy(route: dict) -> str:
    proxies = list((route or {}).get("proxies") or [])
    return random.choice(proxies) if proxies else ""


# ============================================================
# 请求/响应工具
# ============================================================

def sanitize_error_text(text: Any, limit: int = 280) -> str:
    """清除 Bearer/JWT、压缩空白并限制长度。"""
    value = str(text or "")
    value = _BEARER_RE.sub("Bearer ***", value)
    value = _JWT_RE.sub("***", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def extract_error_message(data: Any, text: str = "") -> str:
    """从 error/message/detail/reason/code/type 字段提取错误详情，失败回退响应文本。"""
    if isinstance(data, dict):
        for key in ("error", "message", "detail", "reason", "code", "type"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return sanitize_error_text(value)
            if isinstance(value, dict):
                inner = extract_error_message(value)
                if inner:
                    return inner
    return sanitize_error_text(text)


def build_checkout_body(country: str, currency: str, promo_campaign_id: str | None = None) -> dict:
    """按线路国家和币种生成优惠 Custom Checkout 请求体。"""
    return {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": str(country or "").upper(),
            "currency": str(currency or "").upper(),
        },
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": str(promo_campaign_id or getattr(cfg, "PAYMENT_PROMO_CAMPAIGN_ID", "plus-1-month-free")),
            "is_coupon_from_query_param": False,
        },
    }


def extract_checkout_session(data: Any, text: str = "") -> str:
    """从建单响应中提取 oaics_/cs_live_/cs_test_ 会话 ID。"""
    candidates: list[str] = []
    if isinstance(data, dict):
        for key in ("checkout_session_id", "checkoutSessionId", "session_id"):
            value = data.get(key)
            if isinstance(value, str):
                candidates.append(value)
        checkout_session = data.get("checkout_session")
        if isinstance(checkout_session, dict):
            for key in ("id", "checkout_session_id", "checkoutSessionId", "session_id"):
                value = checkout_session.get(key)
                if isinstance(value, str):
                    candidates.append(value)
    candidates.append(str(text or ""))
    for candidate in candidates:
        match = _SESSION_ID_RE.search(candidate)
        if match:
            return match.group(1)
    return ""


def is_custom_checkout_session(session_id: str) -> bool:
    return str(session_id or "").startswith("oaics_")


def is_hosted_checkout_session(session_id: str) -> bool:
    value = str(session_id or "")
    return value.startswith("cs_live_") or value.startswith("cs_test_")


def extract_processor(data: Any, country: str = "") -> str:
    """提取 OAICS processor；找不到时按国家回退（US→openai_llc，其他→openai_ie）。"""
    found = ""
    if isinstance(data, dict):
        for key in ("processor_entity", "processorEntity"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                found = value.strip()
                break
        if not found:
            url = str(data.get("checkout_url") or data.get("url") or "")
            match = re.search(r"/checkout/([A-Za-z0-9_]+)/", url)
            if match:
                found = match.group(1)
    if found:
        return found
    return "openai_llc" if str(country or "").upper() == "US" else "openai_ie"


def normalize_payment_method_id(value: Any) -> str:
    method = str(value or "").strip().lower()
    method = method.replace("-", "_").replace(" ", "_")
    while "__" in method:
        method = method.replace("__", "_")
    return _METHOD_ALIASES.get(method, method)


def _iter_payment_method_values(obj: Any, depth: int = 0):
    if depth > 8:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _PAYMENT_FIELD_KEYS:
                yield from _method_entries(value, depth + 1)
            elif isinstance(value, (dict, list)):
                yield from _iter_payment_method_values(value, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_payment_method_values(item, depth + 1)


def _method_entries(value: Any, depth: int = 0):
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _method_entries(item, depth + 1)
    elif isinstance(value, dict):
        for key in ("type", "payment_method_type", "name", "label", "display_name", "id"):
            picked = value.get(key)
            if isinstance(picked, str) and picked.strip():
                yield picked.strip()
                break
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _method_entries(item, depth + 1)


def _has_custom_payment_methods(obj: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "custom_payment_methods" and value:
                return True
            if isinstance(value, (dict, list)) and _has_custom_payment_methods(value, depth + 1):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)) and _has_custom_payment_methods(item, depth + 1):
                return True
    return False


def parse_payment_methods(payload: Any, *, country: str = "", currency: str = "") -> list[str]:
    """解析、归一化并去重支付方式，保持首次出现顺序。"""
    if not isinstance(payload, (dict, list)):
        return []
    methods: list[str] = []
    for value in _iter_payment_method_values(payload):
        method = normalize_payment_method_id(value)
        if not method or method.startswith("cpmt_"):
            # cpmt_... 是不透明 Custom Payment Method，不能可靠证明具体钱包类型。
            continue
        if method not in methods:
            methods.append(method)
    try:
        text = json.dumps(payload, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        text = ""
    if '"paypal"' in text and "paypal" not in methods:
        methods.append("paypal")
    # 菲律宾线路：出现不透明 Custom Payment Method 时补充推断 gcash（文档同款启发式）。
    if str(country or "").upper() == "PH" and str(currency or "").upper() == "PHP":
        if "gcash" not in methods and _has_custom_payment_methods(payload):
            methods.append("gcash")
    return methods


def classify_checkout_response(status_code: int, text: str, data: Any) -> tuple[str, str, str]:
    """对非 2xx 建单响应分类，返回 (status, state, error)。"""
    status = int(status_code or 0)
    error = extract_error_message(data, text)
    lower = str(text or "").lower()
    if status == 400:
        if "already paid" in lower or "already_paid" in lower:
            return "already_paid", "already_paid", error
        return "checkout_rejected", "checkout_rejected", error
    if status == 401:
        return "token_invalid", "token_invalid", "AT已过期/失效，请手动查活刷新"
    if status == 403:
        return "risk_blocked", "risk_blocked", error
    if status == 429:
        return "rate_limited", "rate_limited", error
    return "unknown", "checkout_failed", error or f"HTTP {status}"


# ============================================================
# 检测客户端
# ============================================================

class PaymentRouteClient:
    """单条区域线路的 HTTP 客户端。

    使用项目现有 BrowserSession（curl_cffi）承载 TLS 指纹、Cookie 与 Sentinel；
    每条线路拥有独立的设备/会话身份与代理。
    """

    def __init__(self, route: dict, proxy: str, email: str = ""):
        seed = f"payment:{email}:{route.get('country')}:{route.get('currency')}:{uuid.uuid4().hex}"
        self.route = route
        self.proxy = proxy
        self.env = BrowserSession(proxy=proxy or "", detect_exit_geo=False, fingerprint_seed=seed)
        self.locale = str(route.get("locale") or "en-US")
        try:
            self.env.session.timeout = float(getattr(cfg, "PAYMENT_CHECK_TIMEOUT_SECONDS", 45.0) or 45.0)
        except (TypeError, ValueError):
            self.env.session.timeout = 45.0
        profile = self.env.browser_profile or {}
        profile["navigator_language"] = self.locale
        profile["accept_language"] = self.locale
        languages = [self.locale]
        for extra in (str(self.locale).split("-")[0], "en-US", "en"):
            if extra and extra.lower() not in {x.lower() for x in languages}:
                languages.append(extra)
        profile["navigator_languages"] = languages
        self.device_id = self.env.device_id
        self.session_id = self.env.oai_session_id

    # ---- 基础请求 ----
    def _base_headers(self) -> dict:
        headers = self.env._get_common_headers()
        headers.update({
            "oai-device-id": self.device_id,
            "oai-session-id": self.session_id,
            "oai-language": self.locale,
            "oai-client-version": OAI_CLIENT_VERSION,
            "oai-client-build-number": OAI_CLIENT_BUILD_NUMBER,
        })
        return headers

    def _navigate_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        headers = self._base_headers()
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "referer": referer,
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "upgrade-insecure-requests": "1",
        })
        return headers

    def _api_headers(self, url: str, referer: str, *, access_token: str = "", account_id: str = "") -> dict:
        headers = self.env.get_chatgpt_headers(referer=referer)
        headers.update(self._base_headers())
        # chatgpt.com 前端 API 的诊断头；直接走 session 时需要手动补齐。
        path = urlparse(str(url or "")).path or "/"
        if path.startswith(("/backend-api/", "/backend-anon/", "/ces/")):
            headers.setdefault("x-openai-target-path", path)
            headers.setdefault("x-openai-target-route", path)
        if access_token:
            token = str(access_token).strip()
            headers["authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        if account_id:
            headers["chatgpt-account-id"] = str(account_id)
        return headers

    def _observe(self, url: str) -> None:
        try:
            self.env._observe_cf_cookie_changes(url)
        except Exception:
            pass

    def get(self, url: str, headers: dict, **kwargs):
        resp = self.env.session.get(url, headers=headers, **kwargs)
        self._observe(url)
        return resp

    def post(self, url: str, headers: dict, **kwargs):
        resp = self.env.session.post(url, headers=headers, **kwargs)
        self._observe(url)
        return resp

    def close(self) -> None:
        try:
            self.env.session.close()
        except Exception:
            pass


# ============================================================
# 分步骤执行
# ============================================================

def _detect_exit_country(client: PaymentRouteClient) -> str:
    url = str(getattr(cfg, "PAYMENT_CHECK_EXIT_URL", "") or "").strip()
    if not url:
        return ""
    resp = client.get(url, headers=client._navigate_headers(), allow_redirects=True)
    text = resp.text or ""
    match = _LOC_RE.search(text)
    return (match.group(1) or "").upper() if match else ""


def _stripe_reachable(client: PaymentRouteClient) -> bool:
    url = str(getattr(cfg, "PAYMENT_STRIPE_PREFLIGHT_URL", "") or "").strip()
    if not url:
        return True
    try:
        client.get(url, headers=client._navigate_headers(referer="https://chatgpt.com/"), allow_redirects=True)
        return True
    except Exception:
        return False


def _warmup(client: PaymentRouteClient) -> int:
    url = str(getattr(cfg, "PAYMENT_WARMUP_URL", "") or "").strip()
    if not url:
        return 200
    resp = client.get(url, headers=client._navigate_headers(), allow_redirects=True)
    return int(getattr(resp, "status_code", 0) or 0)


def _check_me(client: PaymentRouteClient, token: str, account_id: str) -> tuple[int, str]:
    url = str(getattr(cfg, "PAYMENT_ME_URL", "") or "").strip()
    if not url:
        return 200, ""
    resp = client.get(
        url,
        headers=client._api_headers(url, "https://chatgpt.com/", access_token=token, account_id=account_id),
        allow_redirects=False,
    )
    return int(getattr(resp, "status_code", 0) or 0), (resp.text or "")


def _sentinel_headers(client: PaymentRouteClient) -> dict:
    """尽力获取 Sentinel 头；失败不阻断检测。"""
    headers: dict[str, str] = {}
    try:
        flow = str(getattr(cfg, "PAYMENT_SENTINEL_FLOW", "checkout_session_creation") or "checkout_session_creation")
        sentinel_resp = request_sentinel_token(client.env, flow)
        if not sentinel_resp:
            return headers
        token_header, so_header = build_sentinel_header(client.env, sentinel_resp, flow)
        if token_header:
            headers["openai-sentinel-token"] = token_header
        if so_header:
            headers["openai-sentinel-so-token"] = so_header
    except Exception as exc:
        logger.debug("[支付检测] Sentinel 获取失败（忽略）: %s: %s", type(exc).__name__, exc)
    return headers


def _create_checkout(client: PaymentRouteClient, token: str, account_id: str, body: dict):
    url = str(getattr(cfg, "PAYMENT_CHECKOUT_URL", "") or "").strip()
    headers = client._api_headers(url, "https://chatgpt.com/", access_token=token, account_id=account_id)
    headers["origin"] = "https://chatgpt.com"
    headers.update(_sentinel_headers(client))
    resp = client.post(url, headers=headers, data=json.dumps(body), allow_redirects=False)
    return resp


def _read_custom_checkout_state(
    client: PaymentRouteClient,
    token: str,
    account_id: str,
    processor: str,
    session_id: str,
) -> tuple[int, Any]:
    base = str(getattr(cfg, "PAYMENT_CHECKOUT_STATE_BASE", "") or "").rstrip("/")
    url = f"{base}/{processor}/{session_id}"
    referer = f"https://chatgpt.com/checkout/{processor}/{session_id}"
    resp = client.get(
        url,
        headers=client._api_headers(url, referer, access_token=token, account_id=account_id),
        allow_redirects=False,
    )
    data = None
    try:
        data = resp.json()
    except Exception:
        data = None
    return int(getattr(resp, "status_code", 0) or 0), data


def _extract_stripe_keys(payload: Any) -> list[str]:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload or "")
    keys: list[str] = []
    for match in _STRIPE_PK_RE.finditer(text):
        key = match.group(0)
        if key not in keys:
            keys.append(key)
    return keys


def _stripe_init(
    client: PaymentRouteClient,
    session_id: str,
    response_keys: list[str],
    checkout_url: str,
) -> tuple[int, Any, str]:
    api_base = str(getattr(cfg, "PAYMENT_STRIPE_API_BASE", "https://api.stripe.com") or "").rstrip("/")
    api_version = str(getattr(cfg, "PAYMENT_STRIPE_API_VERSION", "2025-03-31.basil") or "")
    keys = list(response_keys or [])
    for key in (getattr(cfg, "PAYMENT_STRIPE_PUBLISHABLE_KEYS", []) or []):
        key = str(key or "").strip()
        if key and key not in keys:
            keys.append(key)
    if not keys and checkout_url:
        # 兜底：从 Stripe 收银台页面 HTML 提取 publishable key。
        try:
            page = client.get(checkout_url, headers=client._navigate_headers(), allow_redirects=True)
            for match in _STRIPE_PK_RE.finditer(page.text or ""):
                key = match.group(0)
                if key not in keys:
                    keys.append(key)
        except Exception as exc:
            logger.debug("[支付检测] 提取 Stripe key 失败: %s: %s", type(exc).__name__, exc)
    if not keys:
        return 0, None, ""
    headers = client._navigate_headers(referer="https://js.stripe.com/")
    headers.update({
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://js.stripe.com",
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    })
    url = f"{api_base}/v1/payment_pages/{session_id}/init"
    last_status = 0
    last_data = None
    for key in keys:
        data = {
            "browser_locale": client.locale,
            "key": key,
            "_stripe_version": api_version,
        }
        resp = client.post(url, headers=headers, data=data, allow_redirects=False)
        last_status = int(getattr(resp, "status_code", 0) or 0)
        try:
            last_data = resp.json()
        except Exception:
            last_data = None
        if last_status == 200:
            return last_status, last_data, key
    return last_status, last_data, ""


def _route_result(route: dict, status: str, state: str, *, methods=None, error="", http_status=0,
                  exit_country="", session_type="") -> dict:
    return {
        "country": route.get("country"),
        "currency": route.get("currency"),
        "locale": route.get("locale"),
        "status": status,
        "methods": list(methods or []),
        "state": state,
        "checkedAt": now_iso(),
        "exit": exit_country or "",
        "error": sanitize_error_text(error) if error else "",
        "httpStatus": int(http_status or 0),
        "sessionType": session_type or "",
    }


def _run_route(route: dict, token: str, account_id: str, email: str) -> dict:
    """执行单条线路检测，返回线路结果 dict。"""
    _wait_route_slot()
    proxy = pick_route_proxy(route)
    client = PaymentRouteClient(route, proxy, email=email)
    exit_country = ""
    session_id = ""
    session_type = ""
    try:
        try:
            exit_country = _detect_exit_country(client)
        except Exception as exc:
            return _route_result(route, "proxy_unavailable", "proxy_unavailable",
                                 error=f"{type(exc).__name__}: {exc}")
        if not exit_country:
            return _route_result(route, "proxy_unavailable", "proxy_unavailable",
                                 error="无法检测代理出口国家")
        if exit_country != str(route.get("country") or "").upper():
            return _route_result(route, "proxy_country_mismatch", "proxy_country_mismatch",
                                 error=f"出口国家 {exit_country} 与线路国家 {route.get('country')} 不一致",
                                 exit_country=exit_country)

        if not _stripe_reachable(client):
            return _route_result(route, "proxy_unavailable", "stripe_unavailable",
                                 error="代理无法访问 Stripe", exit_country=exit_country)

        try:
            warmup_status = _warmup(client)
        except Exception as exc:
            return _route_result(route, "unknown", "warmup_failed",
                                 error=f"{type(exc).__name__}: {exc}", exit_country=exit_country)
        if warmup_status == 403:
            return _route_result(route, "risk_blocked", "warmup_blocked",
                                 error="ChatGPT 首页预热被安全校验拦截", http_status=warmup_status,
                                 exit_country=exit_country)

        try:
            me_status, me_text = _check_me(client, token, account_id)
        except Exception as exc:
            return _route_result(route, "unknown", "me_failed",
                                 error=f"{type(exc).__name__}: {exc}", exit_country=exit_country)
        if me_status == 401:
            return _route_result(route, "token_invalid", "token_invalid",
                                 error="AT已过期/失效，请手动查活刷新", http_status=me_status,
                                 exit_country=exit_country)
        if me_status == 403:
            return _route_result(route, "risk_blocked", "me_blocked", http_status=me_status,
                                 error=extract_error_message(None, me_text) or "Token 校验被安全校验拦截",
                                 exit_country=exit_country)
        if me_status == 429:
            return _route_result(route, "rate_limited", "me_rate_limited", http_status=me_status,
                                 error=extract_error_message(None, me_text) or "Token 校验被限流",
                                 exit_country=exit_country)
        if not (200 <= me_status < 300):
            return _route_result(route, "unknown", "me_failed", http_status=me_status,
                                 error=extract_error_message(None, me_text) or f"HTTP {me_status}",
                                 exit_country=exit_country)

        body = build_checkout_body(route.get("country"), route.get("currency"))
        try:
            resp = _create_checkout(client, token, account_id, body)
        except Exception as exc:
            return _route_result(route, "unknown", "checkout_failed",
                                 error=f"{type(exc).__name__}: {exc}", exit_country=exit_country)

        http_status = int(getattr(resp, "status_code", 0) or 0)
        response_text = resp.text or ""
        try:
            response_data = resp.json()
        except Exception:
            response_data = None
        if not (200 <= http_status < 300):
            status, state, error = classify_checkout_response(http_status, response_text, response_data)
            return _route_result(route, status, state, error=error, http_status=http_status,
                                 exit_country=exit_country)
        if not isinstance(response_data, dict):
            return _route_result(route, "unknown", "invalid_response", http_status=http_status,
                                 error="建单响应不是 JSON 对象: " + sanitize_error_text(response_text, 120),
                                 exit_country=exit_country)

        # 建单响应里的支付方式与状态响应合并；Session 读取失败不覆盖已有结果。
        methods = parse_payment_methods(response_data, country=route.get("country"), currency=route.get("currency"))

        session_id = extract_checkout_session(response_data, response_text)
        if not session_id:
            return _route_result(route, "unknown", "invalid_checkout_session", http_status=http_status,
                                 methods=methods, error="Checkout 未返回受支持的会话 ID",
                                 exit_country=exit_country)
        session_type = ""
        for prefix in ("oaics_", "cs_live_", "cs_test_"):
            if session_id.startswith(prefix):
                session_type = prefix
                break

        checkout_url = str(response_data.get("checkout_url") or response_data.get("url") or "")

        if is_custom_checkout_session(session_id):
            processor = extract_processor(response_data, route.get("country"))
            try:
                state_status, state_data = _read_custom_checkout_state(client, token, account_id, processor, session_id)
            except Exception as exc:
                state_status, state_data = 0, None
                logger.debug("[支付检测] OAICS 状态读取异常: %s: %s", type(exc).__name__, exc)
            if state_status and not (200 <= state_status < 300):
                if not methods:
                    error = extract_error_message(state_data) or f"HTTP {state_status}"
                    return _route_result(route, "unknown", "state_failed", http_status=state_status,
                                         error=error, exit_country=exit_country, session_type=session_type)
            elif isinstance(state_data, dict):
                for method in parse_payment_methods(state_data, country=route.get("country"), currency=route.get("currency")):
                    if method not in methods:
                        methods.append(method)
        elif is_hosted_checkout_session(session_id):
            response_keys = _extract_stripe_keys(response_data)
            try:
                stripe_status, stripe_data, _used_key = _stripe_init(client, session_id, response_keys, checkout_url)
            except Exception as exc:
                return _route_result(route, "unknown", "stripe_failed", http_status=http_status,
                                     methods=methods, error=f"{type(exc).__name__}: {exc}",
                                     exit_country=exit_country, session_type=session_type)
            if stripe_status == 200 and isinstance(stripe_data, dict):
                for method in parse_payment_methods(stripe_data, country=route.get("country"), currency=route.get("currency")):
                    if method not in methods:
                        methods.append(method)
            elif not methods:
                return _route_result(route, "unknown", "stripe_init_failed", http_status=stripe_status,
                                     error=extract_error_message(stripe_data) or f"Stripe init HTTP {stripe_status}",
                                     exit_country=exit_country, session_type=session_type)
        else:
            return _route_result(route, "unknown", "invalid_checkout_session", http_status=http_status,
                                 methods=methods, error="Checkout 返回了不支持的会话类型",
                                 exit_country=exit_country)

        if methods:
            return _route_result(route, "available", "payment_methods_available", methods=methods,
                                 http_status=http_status, exit_country=exit_country, session_type=session_type)
        return _route_result(route, "not_returned", "payment_methods_empty", http_status=http_status,
                             exit_country=exit_country, session_type=session_type)
    finally:
        client.close()


def check_account_payment_methods(
    access_token: str,
    email: str = "",
    *,
    routes: list[dict] | None = None,
) -> dict:
    """按区域线路依次检测账号可用支付方式，并聚合结果。"""
    checked_at = now_iso()
    if routes is None:
        routes, _errors = parse_payment_routes()
    routes = list(routes or [])
    result: dict = {
        "ok": False,
        "status": "disabled",
        "state": "no_proxy_routes",
        "methods": [],
        "checked_at": checked_at,
        "routes": [],
        "country": "",
        "currency": "",
        "exit": "",
        "http_status": 0,
        "session_type": "",
        "error": "未配置有效的支付检测线路",
        "email": email,
    }
    if not routes:
        return result

    token = str(access_token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        result.update({"status": "token_invalid", "state": "token_invalid", "error": "账号缺少 access_token"})
        return result
    try:
        account_id = str(token_claims(token).get("account_id") or "")
    except Exception:
        account_id = ""

    completed = 0
    for route in routes:
        try:
            route_result = _run_route(route, token, account_id, email)
        except Exception as exc:
            logger.exception("[支付检测] 线路执行异常: %s", route.get("country"))
            route_result = _route_result(route, "unknown", "route_failed", error=f"{type(exc).__name__}: {exc}")
        result["routes"].append(route_result)
        for method in route_result.get("methods") or []:
            if method not in result["methods"]:
                result["methods"].append(method)
        for key, target in (("exit", "exit"), ("currency", "currency"), ("country", "country"),
                            ("sessionType", "session_type")):
            value = route_result.get(key)
            if value:
                result[target] = value
        http_status = int(route_result.get("httpStatus") or 0)
        if http_status:
            result["http_status"] = http_status
        if route_result.get("state") in COMPLETED_STATES:
            completed += 1
        if route_result.get("status") in ("token_invalid", "already_paid"):
            break

    if result["methods"]:
        result["status"], result["state"] = "available", "payment_methods_available"
        if completed != len(result["routes"]):
            result["status"], result["state"] = "partial", "partial"
    elif completed and completed == len(result["routes"]):
        result["status"], result["state"] = "not_returned", "payment_methods_empty"
    else:
        last = result["routes"][-1] if result["routes"] else None
        if last:
            result["status"] = last.get("status") or "unknown"
            result["state"] = last.get("state") or "unknown"
            result["error"] = last.get("error") or result["error"]
        else:
            result["status"], result["state"] = "unknown", "unknown"

    result["ok"] = result["status"] in SUCCESS_STATUSES
    result["email"] = email
    return result
