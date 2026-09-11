# -*- coding: utf-8 -*-
"""支付类型检测配置。

区域线路格式（每行一个）：

    国家|币种|语言|代理

示例：

    DE|EUR|de-DE|http://user:pass@de-proxy.example:3010
    VN|VND|vi-VN|proxy-a.example:3010:user:pass
    US|USD|en-US|socks5://user:pass@us-proxy.example:1080

规则：
    - 国家必须是两位大写字母，币种必须是三位大写字母，语言不能为空；
    - 代理支持 http / https / socks5 / socks5h 以及 host:port:user:pass、
      user:pass@host:port 写法；
    - DIRECT 或空代理视为禁用线路，绝不会回退到机器直连；
    - 相同 国家+币种+语言 的多行会合并成一个代理池，每次检测随机选一个；
    - 最多支持 PAYMENT_CHECK_MAX_ROUTES 组区域线路。
"""
from config.env_loader import apply_env_overrides


# 区域线路（国家|币种|语言|代理）；为空时支付检测直接返回 disabled/no_proxy_routes。
PAYMENT_PROXY_ROUTES: list[str] = []

# 最多支持的区域线路组数，超过会在解析阶段报错。
PAYMENT_CHECK_MAX_ROUTES = 12

# 线路探测启动限速：默认最小间隔 3000ms + 0-500ms 抖动，限制的是线路启动频率，
# 而不是线路内部每一次 HTTP 请求。
PAYMENT_CHECK_MIN_INTERVAL_MS = 3000
PAYMENT_CHECK_JITTER_MS = 500

# 单个检测 HTTP 客户端的请求超时（秒）。
PAYMENT_CHECK_TIMEOUT_SECONDS = 45.0

# 各接口地址（可被 .env 覆盖，便于上游路径变化时快速调整）。
PAYMENT_CHECK_EXIT_URL = "https://cloudflare.com/cdn-cgi/trace"
PAYMENT_WARMUP_URL = "https://chatgpt.com/"
PAYMENT_ME_URL = "https://chatgpt.com/backend-api/me"
PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
PAYMENT_CHECKOUT_STATE_BASE = "https://chatgpt.com/backend-api/payments/checkout"
PAYMENT_STRIPE_PREFLIGHT_URL = "https://api.stripe.com/"
PAYMENT_STRIPE_API_BASE = "https://api.stripe.com"
PAYMENT_STRIPE_API_VERSION = "2025-03-31.basil"

# 额外 Stripe publishable keys（可选）。检测会优先使用 Checkout 响应里的 key，
# 其次是这里配置的 key，最后尝试从 Stripe 收银台页面 HTML 中提取。
PAYMENT_STRIPE_PUBLISHABLE_KEYS: list[str] = []

# 建单使用的促销活动 ID 与 Sentinel flow 标识。
PAYMENT_PROMO_CAMPAIGN_ID = "plus-1-month-free"
PAYMENT_SENTINEL_FLOW = "checkout_session_creation"


apply_env_overrides(globals(), {
    "PAYMENT_PROXY_ROUTES": "list_str_multiline",
    "PAYMENT_CHECK_MAX_ROUTES": "int",
    "PAYMENT_CHECK_MIN_INTERVAL_MS": "int",
    "PAYMENT_CHECK_JITTER_MS": "int",
    "PAYMENT_CHECK_TIMEOUT_SECONDS": "float",
    "PAYMENT_CHECK_EXIT_URL": "str",
    "PAYMENT_WARMUP_URL": "str",
    "PAYMENT_ME_URL": "str",
    "PAYMENT_CHECKOUT_URL": "str",
    "PAYMENT_CHECKOUT_STATE_BASE": "str",
    "PAYMENT_STRIPE_PREFLIGHT_URL": "str",
    "PAYMENT_STRIPE_API_BASE": "str",
    "PAYMENT_STRIPE_API_VERSION": "str",
    "PAYMENT_STRIPE_PUBLISHABLE_KEYS": "list_str_multiline",
    "PAYMENT_PROMO_CAMPAIGN_ID": "str",
    "PAYMENT_SENTINEL_FLOW": "str",
})
