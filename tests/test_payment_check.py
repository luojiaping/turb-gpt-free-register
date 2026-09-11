# -*- coding: utf-8 -*-
import unittest

from config import payment as payment_cfg
from core.payment_check import (
    build_checkout_body,
    classify_checkout_response,
    extract_checkout_session,
    extract_processor,
    is_custom_checkout_session,
    is_hosted_checkout_session,
    normalize_payment_method_id,
    normalize_route_proxy,
    parse_payment_methods,
    parse_payment_routes,
    sanitize_error_text,
)
from webui.config_editor import EDITABLE_FIELDS


class PaymentRouteConfigTests(unittest.TestCase):
    def test_normalize_route_proxy_variants(self):
        self.assertEqual(normalize_route_proxy("DIRECT"), None)
        self.assertEqual(normalize_route_proxy(""), None)
        self.assertEqual(normalize_route_proxy("http://u:p@h:1"), "http://u:p@h:1")
        self.assertEqual(normalize_route_proxy("socks5h://u:p@h:1"), "socks5h://u:p@h:1")
        self.assertEqual(normalize_route_proxy("u:p@h:1"), "http://u:p@h:1")
        self.assertEqual(normalize_route_proxy("h:3010"), "http://h:3010")
        self.assertEqual(normalize_route_proxy("h:3010:u:p"), "http://u:p@h:3010")
        self.assertEqual(normalize_route_proxy("not-a-proxy"), None)

    def test_parse_payment_routes_groups_and_validates(self):
        routes, errors = parse_payment_routes([
            "VN|VND|vi-VN|http://u:p@a:1",
            "vn|vnd|vi-VN|http://u:p@b:2",
            "DE|EUR|de-DE|http://u:p@c:3",
            "US|USD|en-US|DIRECT",
            "BAD|US|en-US|http://h:1",
            "VN|VN|vi-VN|http://h:1",
            "US|USD||http://h:1",
            "",
            "# comment",
        ])
        self.assertEqual(len(routes), 2)
        vn = routes[0]
        self.assertEqual((vn["country"], vn["currency"], vn["locale"]), ("VN", "VND", "vi-VN"))
        self.assertEqual(len(vn["proxies"]), 2)
        de = routes[1]
        self.assertEqual(de["country"], "DE")
        # DIRECT、国家/币种非法、语言为空都会记录错误，而不是生成线路
        self.assertEqual(len(errors), 4)

    def test_parse_payment_routes_max_limit(self):
        routes, errors = parse_payment_routes(
            [f"A{chr(65 + i)}|USD|en-US|http://h:{i}" for i in range(5)],
        )
        self.assertEqual(len(routes), 5)

    def test_config_defaults(self):
        self.assertEqual(payment_cfg.PAYMENT_PROMO_CAMPAIGN_ID, "plus-1-month-free")
        self.assertEqual(payment_cfg.PAYMENT_STRIPE_API_VERSION, "2025-03-31.basil")
        self.assertEqual(payment_cfg.PAYMENT_CHECK_MAX_ROUTES, 12)

    def test_config_editor_exposes_payment_fields(self):
        keys = {item["key"]: item for item in EDITABLE_FIELDS}
        self.assertEqual(keys["PAYMENT_PROXY_ROUTES"]["type"], "list_str_multiline")
        self.assertEqual(keys["PAYMENT_PROXY_ROUTES"]["file"], "payment.py")
        self.assertEqual(keys["PAYMENT_CHECK_TIMEOUT_SECONDS"]["type"], "float")


class CheckoutPayloadTests(unittest.TestCase):
    def test_build_checkout_body(self):
        body = build_checkout_body("VN", "VND")
        self.assertEqual(body["entry_point"], "all_plans_pricing_modal")
        self.assertEqual(body["plan_name"], "chatgptplusplan")
        self.assertEqual(body["billing_details"], {"country": "VN", "currency": "VND"})
        self.assertEqual(body["checkout_ui_mode"], "custom")
        self.assertEqual(body["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")
        self.assertIs(body["promo_campaign"]["is_coupon_from_query_param"], False)

    def test_extract_checkout_session(self):
        self.assertEqual(extract_checkout_session({"checkout_session_id": "oaics_abc123"}, ""), "oaics_abc123")
        self.assertEqual(extract_checkout_session({"checkoutSessionId": "cs_live_xyz"}, ""), "cs_live_xyz")
        self.assertEqual(extract_checkout_session({"checkout_session": {"id": "cs_test_1"}}, ""), "cs_test_1")
        self.assertEqual(extract_checkout_session(None, "url=/checkout/session/oaics_fromtext"), "oaics_fromtext")
        self.assertEqual(extract_checkout_session({}, "no session here"), "")

    def test_session_type_checks(self):
        self.assertTrue(is_custom_checkout_session("oaics_x"))
        self.assertFalse(is_custom_checkout_session("cs_live_x"))
        self.assertTrue(is_hosted_checkout_session("cs_live_x"))
        self.assertTrue(is_hosted_checkout_session("cs_test_x"))
        self.assertFalse(is_hosted_checkout_session("oaics_x"))

    def test_extract_processor(self):
        self.assertEqual(extract_processor({"processor_entity": "openai_llc"}, "US"), "openai_llc")
        self.assertEqual(extract_processor({"processorEntity": "openai_ie"}, "DE"), "openai_ie")
        self.assertEqual(extract_processor({"checkout_url": "https://x/checkout/openai_ie/oaics_1"}, "DE"), "openai_ie")
        self.assertEqual(extract_processor({}, "US"), "openai_llc")
        self.assertEqual(extract_processor({}, "VN"), "openai_ie")

    def test_classify_checkout_response(self):
        self.assertEqual(classify_checkout_response(400, "item already paid", None)[0], "already_paid")
        self.assertEqual(classify_checkout_response(400, "bad request", None)[0], "checkout_rejected")
        self.assertEqual(classify_checkout_response(401, "", None)[0], "token_invalid")
        self.assertEqual(classify_checkout_response(403, "cf", None)[0], "risk_blocked")
        self.assertEqual(classify_checkout_response(429, "", None)[0], "rate_limited")
        self.assertEqual(classify_checkout_response(500, "", None)[1], "checkout_failed")

    def test_sanitize_error_text_strips_credentials(self):
        text = "Bearer eyJhbGciOi.eyJhYmMi.SIG and token abc.def.ghi " * 10
        out = sanitize_error_text(text, limit=280)
        self.assertNotIn("Bearer eyJ", out)
        self.assertLessEqual(len(out), 280)


class PaymentMethodParseTests(unittest.TestCase):
    def test_normalize_payment_method_id(self):
        self.assertEqual(normalize_payment_method_id("Card_Payment"), "card")
        self.assertEqual(normalize_payment_method_id("direct-card"), "card")
        self.assertEqual(normalize_payment_method_id("Kakao"), "kakao_pay")
        self.assertEqual(normalize_payment_method_id("go pay"), "gopay")
        self.assertEqual(normalize_payment_method_id("Grab-Pay"), "grabpay")
        self.assertEqual(normalize_payment_method_id("PayPal"), "paypal")

    def test_parse_payment_methods_types_and_specs(self):
        payload = {
            "payment_method_types": ["card", "momo"],
            "payment_method_specs": [{"type": "card"}, {"payment_method_type": "momo"}],
        }
        self.assertEqual(parse_payment_methods(payload), ["card", "momo"])

    def test_parse_payment_methods_order_and_dedupe(self):
        payload = {
            "ordered_payment_method_types": ["momo", "card", "momo"],
            "payment_method_specs": [
                {"type": "card"},
                {"name": "GCash"},
            ],
        }
        self.assertEqual(parse_payment_methods(payload), ["momo", "card", "gcash"])

    def test_parse_payment_methods_ignores_opaque_cpmt(self):
        payload = {"custom_payment_methods": [{"id": "cpmt_123"}, {"type": "card"}]}
        self.assertEqual(parse_payment_methods(payload), ["card"])

    def test_parse_payment_methods_paypal_supplement(self):
        payload = {"payment_method_types": ["card"], "somewhere": {"paypal": {"enabled": True}}}
        self.assertEqual(parse_payment_methods(payload), ["card", "paypal"])

    def test_parse_payment_methods_ph_gcash_heuristic(self):
        payload = {"custom_payment_methods": [{"id": "cpmt_opaque"}]}
        self.assertEqual(parse_payment_methods(payload, country="PH", currency="PHP"), ["gcash"])
        # 其他地区不做同类推断
        self.assertEqual(parse_payment_methods(payload, country="VN", currency="VND"), [])


class PaymentServiceTests(unittest.TestCase):
    def test_submit_rejects_empty_ids(self):
        from core import payment_check_service as svc

        result = svc.submit_payment_check([])
        self.assertFalse(result["accepted"])
        self.assertIn("account_ids", result["error"])

    def test_submit_returns_busy_when_batch_active(self):
        from core import payment_check_service as svc

        original = svc._BATCH_ACTIVE
        svc._BATCH_ACTIVE = True
        try:
            result = svc.submit_payment_check([1])
            self.assertTrue(result["busy"])
            self.assertFalse(result["accepted"])
        finally:
            svc._BATCH_ACTIVE = original

    def test_queue_settings_shape(self):
        from core import payment_check_service as svc

        settings = svc.queue_settings()
        self.assertEqual(settings["workers"], 1)
        self.assertIn("min_interval_ms", settings)
        self.assertIn("jitter_ms", settings)


if __name__ == "__main__":
    unittest.main()
