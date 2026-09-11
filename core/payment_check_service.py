# -*- coding: utf-8 -*-
"""支付类型检测后台服务。

保守的串行模型：整个服务同一时间只允许一个支付检测批次；批次中的账号按请求
ID 顺序执行，每个账号的区域线路按配置顺序执行，线路之间经过全局速率槽。
"""
from __future__ import annotations

import logging
import threading

from config import payment as cfg
from core import db
from core.payment_check import check_account_payment_methods, now_iso, parse_payment_routes

logger = logging.getLogger(__name__)

_BATCH_LOCK = threading.Lock()
_BATCH_ACTIVE = False


def is_active() -> bool:
    with _BATCH_LOCK:
        return _BATCH_ACTIVE


def queue_settings() -> dict:
    try:
        min_interval_ms = max(0, int(float(getattr(cfg, "PAYMENT_CHECK_MIN_INTERVAL_MS", 3000) or 0)))
    except (TypeError, ValueError):
        min_interval_ms = 3000
    try:
        jitter_ms = max(0, int(float(getattr(cfg, "PAYMENT_CHECK_JITTER_MS", 500) or 0)))
    except (TypeError, ValueError):
        jitter_ms = 500
    return {"workers": 1, "min_interval_ms": min_interval_ms, "jitter_ms": jitter_ms}


def submit_payment_check(account_ids: list | None, trigger: str = "manual") -> dict:
    """提交一次支付类型检测批次。同一时间只允许一个批次。"""
    global _BATCH_ACTIVE
    ids: list[int] = []
    seen: set[int] = set()
    for raw in account_ids or []:
        try:
            acc_id = int(raw)
        except (TypeError, ValueError):
            continue
        if acc_id in seen:
            continue
        seen.add(acc_id)
        ids.append(acc_id)
    if not ids:
        return {"accepted": False, "error": "account_ids 必须是非空数组"}
    if len(ids) > 500:
        return {"accepted": False, "error": "单次最多检测 500 个账号"}

    with _BATCH_LOCK:
        if _BATCH_ACTIVE:
            return {"accepted": False, "busy": True, "error": "支付类型检测正在执行中，请稍后再试"}
        _BATCH_ACTIVE = True

    claimed: list[dict] = []
    skipped: list[dict] = []
    try:
        for acc_id in ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not str(acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            if not db.claim_account_payment_check(acc_id, trigger=trigger):
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "该账号正在检测或状态占用"})
                continue
            claimed.append({"id": acc_id, "email": acc.get("email")})

        if not claimed:
            with _BATCH_LOCK:
                _BATCH_ACTIVE = False
            return {"accepted": False, "error": "没有可检测的账号", "skipped": skipped}

        thread = threading.Thread(
            target=_run_batch,
            args=([item["id"] for item in claimed], trigger),
            name="payment-check-batch",
            daemon=True,
        )
        thread.start()
        return {
            "accepted": True,
            "started": claimed,
            "started_count": len(claimed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }
    except Exception:
        with _BATCH_LOCK:
            _BATCH_ACTIVE = False
        raise


def _run_batch(account_ids: list[int], trigger: str) -> None:
    global _BATCH_ACTIVE
    try:
        routes, route_errors = parse_payment_routes()
        if route_errors:
            logger.warning(
                "[支付检测] 线路配置存在 %s 条问题: %s",
                len(route_errors), "; ".join(route_errors[:5]),
            )
        if not routes:
            error = "未配置有效的支付检测线路"
            if route_errors:
                error = f"{error}：{route_errors[0]}"
            for acc_id in account_ids:
                db.update_account_payment_check(acc_id=acc_id, result={
                    "ok": False,
                    "status": "disabled",
                    "state": "no_proxy_routes",
                    "methods": [],
                    "checked_at": now_iso(),
                    "error": error,
                })
            return

        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                continue
            if not db.mark_account_payment_check_running(acc_id):
                continue
            try:
                result = check_account_payment_methods(
                    acc.get("access_token") or "",
                    email=acc.get("email") or "",
                    routes=routes,
                )
            except Exception as exc:
                logger.exception("[支付检测] 账号检测异常: %s", acc.get("email"))
                result = {
                    "ok": False,
                    "status": "unknown",
                    "state": "failed",
                    "methods": [],
                    "checked_at": now_iso(),
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            try:
                db.update_account_payment_check(acc_id=acc_id, result=result)
            except Exception:
                logger.exception("[支付检测] 结果落库失败: %s", acc.get("email"))
            logger.info(
                "[支付检测] 完成: %s, status=%s, state=%s, methods=%s",
                acc.get("email"), result.get("status"), result.get("state"), result.get("methods"),
            )
    finally:
        with _BATCH_LOCK:
            _BATCH_ACTIVE = False
