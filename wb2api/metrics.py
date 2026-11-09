"""请求级指标：TTFB 分位、outcome 分布、重试次数直方图、按模型延迟。

为什么不用直方图库：网关零依赖，而且这里只需要"够用的分位数"。用固定容量的
环形缓冲存最近 N 个样本，按需排序取分位——N=2000 时排序不到 1ms，面板每 5 秒
拉一次完全扛得住。

为什么单独一层而不复用台账：台账是钱的账（保留 7 天、可回放），指标是"最近怎么样"
的账（只要最近几分钟、重启即清空）。把两者混在一起，要么台账被指标样本撑爆，
要么指标被 7 天前的数据拖偏。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

DEFAULT_CAPACITY = 2000
# outcome 只有这四种：跑完、客户端跑了、上游断了、根本没跑起来。
OUTCOMES = ("completed", "client_aborted", "upstream_aborted", "failed")


def _percentile(sorted_values: List[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    index = int(round((len(sorted_values) - 1) * ratio))
    return round(float(sorted_values[max(0, min(index, len(sorted_values) - 1))]), 1)


class Metrics:
    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self.capacity = max(50, int(capacity or DEFAULT_CAPACITY))
        self._lock = threading.Lock()
        self._samples: List[Dict[str, Any]] = []
        self._outcomes: Dict[str, int] = {name: 0 for name in OUTCOMES}
        self._attempts: Dict[str, int] = {}  # "1" / "2" / "3" / "4+"
        self._status: Dict[str, int] = {}  # "2xx" / "4xx" / "5xx" / "none"
        self._models: Dict[str, Dict[str, Any]] = {}
        self._cache_hit = 0
        self._cache_known = 0
        self._started_at = time.time()

    # -- ingest -------------------------------------------------------------
    def observe(self, record: Dict[str, Any]) -> None:
        # service 写的键是 ttft_ms；老台账/测试里是 ttfb_ms。只认一个的后果是
        # 首字延迟永远是 0——面板上"首字 p50 = 0ms"看着像好消息，其实是没读到。
        ttfb_raw = record.get("ttft_ms", record.get("ttfb_ms"))
        # 非流式请求没有首字（None）：当成 0 混进分位数，p50 会被拉成"快得不可能"。
        ttfb = float(ttfb_raw) if ttfb_raw else None
        elapsed = float(record.get("elapsed_ms") or record.get("latency_ms") or 0)
        outcome = str(record.get("outcome") or "completed")
        if outcome not in self._outcomes:
            outcome = "failed"
        status = record.get("status")
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = 0
        bucket = "none" if code <= 0 else "%dxx" % (code // 100)
        attempts = int(record.get("attempts") or 1)
        attempt_key = "4+" if attempts >= 4 else str(max(1, attempts))
        cached = int(record.get("cached_tokens") or 0)
        prompt = int(record.get("prompt_tokens") or 0)
        model = str(record.get("model") or "")

        with self._lock:
            self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
            self._attempts[attempt_key] = self._attempts.get(attempt_key, 0) + 1
            self._status[bucket] = self._status.get(bucket, 0) + 1
            if prompt > 0:
                self._cache_known += 1
                if cached > 0:
                    self._cache_hit += 1
            if model:
                slot = self._models.setdefault(
                    model,
                    {"requests": 0, "errors": 0, "ttfb_total": 0.0, "ttfb_samples": 0, "elapsed_total": 0.0},
                )
                slot["requests"] += 1
                if bucket in ("4xx", "5xx", "none"):
                    slot["errors"] += 1
                if ttfb is not None:
                    slot["ttfb_total"] += ttfb
                    slot["ttfb_samples"] += 1
                slot["elapsed_total"] += elapsed
            self._samples.append({"ttfb": ttfb, "elapsed": elapsed, "model": model, "ok": bucket == "2xx"})
            del self._samples[:-self.capacity]

    # -- views --------------------------------------------------------------
    def snapshot(self, models_limit: int = 20) -> Dict[str, Any]:
        with self._lock:
            samples = list(self._samples)
            outcomes = dict(self._outcomes)
            attempts = dict(self._attempts)
            status = dict(self._status)
            models = {name: dict(slot) for name, slot in self._models.items()}
            cache_hit, cache_known = self._cache_hit, self._cache_known
            started = self._started_at

        # 只统计"真的有首字"的样本（非流式请求没有），否则分位数会被 0 拉偏。
        ttfb = sorted(sample["ttfb"] for sample in samples if sample["ttfb"] is not None)
        elapsed = sorted(sample["elapsed"] for sample in samples)
        total = len(samples)
        errors = sum(1 for sample in samples if not sample["ok"])
        model_rows = []
        for name, slot in models.items():
            requests = max(0, int(slot["requests"]))
            model_rows.append({
                "model": name,
                "requests": requests,
                "errors": int(slot["errors"]),
                "avg_ttfb_ms": (
                    round(slot["ttfb_total"] / slot["ttfb_samples"], 1) if slot["ttfb_samples"] else None
                ),
                "avg_elapsed_ms": round(slot["elapsed_total"] / requests, 1) if requests else 0.0,
            })
        model_rows.sort(key=lambda row: (-row["requests"], row["model"]))
        return {
            "window": {
                "samples": total,
                "capacity": self.capacity,
                "uptime_seconds": round(time.time() - started, 1),
            },
            "requests": {
                "sampled": total,
                "errors": errors,
                "error_rate": round(errors / total, 4) if total else 0.0,
                "cache_hit_rate": round(cache_hit / cache_known, 4) if cache_known else 0.0,
            },
            "ttfb_ms": {
                "p50": _percentile(ttfb, 0.50),
                "p90": _percentile(ttfb, 0.90),
                "p99": _percentile(ttfb, 0.99),
                "max": round(ttfb[-1], 1) if ttfb else 0.0,
            },
            "elapsed_ms": {
                "p50": _percentile(elapsed, 0.50),
                "p90": _percentile(elapsed, 0.90),
                "p99": _percentile(elapsed, 0.99),
            },
            "outcomes": outcomes,
            "attempts": dict(sorted(attempts.items())),
            "status": dict(sorted(status.items())),
            "models": model_rows[:models_limit],
        }

    def reset(self) -> None:
        with self._lock:
            self._samples = []
            self._outcomes = {name: 0 for name in OUTCOMES}
            self._attempts = {}
            self._status = {}
            self._models = {}
            self._cache_hit = 0
            self._cache_known = 0
            self._started_at = time.time()
