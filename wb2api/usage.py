"""Usage ledger: append-only JSONL plus in-memory aggregates for the panel.

Why JSONL and not SQLite: the gateway must stay dependency-free and a single
process owns the file.  Records are one line each, so ``tail``/``grep`` work for
debugging, and rotation is a file rename instead of a VACUUM.

Aggregation is done in memory at startup by replaying the retained files (a few
hundred thousand lines at most with the default 7-day retention), then kept up
to date incrementally.  Every view is bounded by ``max_days``.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from . import util
from .log import get_logger

log = get_logger("usage")


class UsageLedger:
    def __init__(self, data_dir: str, retention_days: int = 7, archive_enabled: bool = True) -> None:
        self.dir = os.path.join(os.path.abspath(data_dir), "usage")
        util.ensure_dir(self.dir)
        self.retention_days = max(1, int(retention_days))
        self.archive_enabled = bool(archive_enabled)
        self._lock = threading.RLock()
        self._recent: List[Dict[str, Any]] = []
        self._counters: Dict[str, Dict[str, float]] = {
            "total": self._empty(),
            "by_day": {},  # type: ignore[dict-item]
            "by_key": {},
            "by_account": {},
            "by_model": {},
        }
        self._loaded_days: set = set()

    # -- record shape -------------------------------------------------------
    @staticmethod
    def _empty() -> Dict[str, float]:
        return {
            "requests": 0,
            "ok": 0,
            "errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "credits": 0,
            "latency_ms": 0,
        }

    @staticmethod
    def _day(ts: Optional[float] = None) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else util.now()))

    def file_for(self, day: str) -> str:
        return os.path.join(self.dir, f"usage-{day}.jsonl")

    # -- ingest -------------------------------------------------------------
    def record(self, event: Dict[str, Any]) -> None:
        record = dict(event)
        record.setdefault("ts", util.now())
        record.setdefault("day", self._day(record["ts"]))
        with self._lock:
            self._ensure_day(str(record["day"]))
            self._apply(record)
            self._recent.append(record)
            del self._recent[:-500]
            try:
                util.append_jsonl(self.file_for(record["day"]), record)
            except OSError as exc:
                log.warning("usage write failed: %s", exc)

    def _apply(self, record: Dict[str, Any]) -> None:
        ok = record.get("status", 200)
        ok = 200 <= int(ok) < 400
        prompt = int(record.get("prompt_tokens") or 0)
        completion = int(record.get("completion_tokens") or 0)
        cached = int(record.get("cached_tokens") or 0)
        # service 写的是单数 credit（上游 usage 就叫这个），历史台账里也有写 credits 的，
        # 两个都认——否则面板上"消耗积分"永远是 0，用户看不出钱花在哪。
        credits = float(record.get("credit") or record.get("credits") or 0)
        latency = float(record.get("latency_ms") or record.get("elapsed_ms") or 0)

        def bump(bucket: Dict[str, float]) -> None:
            bucket["requests"] += 1
            bucket["ok" if ok else "errors"] += 1
            bucket["prompt_tokens"] += prompt
            bucket["completion_tokens"] += completion
            bucket["cached_tokens"] += cached
            bucket["credits"] += credits
            bucket["latency_ms"] += latency

        bump(self._counters["total"])
        day = record.get("day") or self._day()
        bump(self._counters["by_day"].setdefault(day, self._empty()))
        if record.get("key_name") or record.get("key"):
            bump(self._counters["by_key"].setdefault(str(record.get("key_name") or record.get("key")), self._empty()))
        if record.get("account"):
            bump(self._counters["by_account"].setdefault(str(record["account"]), self._empty()))
        if record.get("model"):
            bump(self._counters["by_model"].setdefault(str(record["model"]), self._empty()))

    # -- startup ------------------------------------------------------------
    def load(self) -> None:
        """Replay retained files once; safe to call twice."""
        with self._lock:
            self._purge_old_files()
            for day in sorted(self._days_on_disk()):
                self._ensure_day(day)
        log.info(
            "usage ledger loaded: %s requests, %s days",
            int(self._counters["total"]["requests"]),
            len(self._loaded_days),
        )

    def _ensure_day(self, day: str) -> None:
        """Replay one day's file exactly once (also when it appears mid-run)."""
        if day in self._loaded_days:
            return
        self._loaded_days.add(day)
        path = self.file_for(day)
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = __import__("json").loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        self._apply(record)
                        # 回放也要进 recent：否则重启后面板的"最近请求"是空的，
                        # 而同一页上的总数却是有值的——这种不一致最容易被当成 bug 报。
                        self._recent.append(record)
                        del self._recent[:-500]
        except OSError:
            pass

    def _days_on_disk(self) -> List[str]:
        days = []
        try:
            for name in os.listdir(self.dir):
                if name.startswith("usage-") and name.endswith(".jsonl"):
                    days.append(name[len("usage-") : -len(".jsonl")])
        except OSError:
            pass
        return days

    def _purge_old_files(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for day in self._days_on_disk():
            try:
                stamp = time.mktime(time.strptime(day, "%Y-%m-%d"))
            except ValueError:
                continue
            if stamp >= cutoff:
                continue
            path = self.file_for(day)
            try:
                os.unlink(path)
                log.info("usage file rotated out: %s", path)
            except OSError:
                pass

    # -- views --------------------------------------------------------------
    def prune(self) -> int:
        """删掉超过保留期的日文件，返回删除个数。"""
        before = len(self._days_on_disk())
        self._purge_old_files()
        return before - len(self._days_on_disk())

    def flush(self) -> None:
        """台账逐条 ``write`` + ``fsync`` 落盘，没有缓冲可刷；保留接口供关闭流程统一调用。"""
        return None

    def recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._recent[-limit:])[::-1]

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            total = dict(self._counters["total"])
            by_day = {day: dict(bucket) for day, bucket in sorted(self._counters["by_day"].items())}
            by_key = {k: dict(v) for k, v in sorted(self._counters["by_key"].items())}
            by_account = {k: dict(v) for k, v in sorted(self._counters["by_account"].items())}
            by_model = {k: dict(v) for k, v in sorted(self._counters["by_model"].items())}
        requests = max(1, int(total["requests"]))
        total["avg_latency_ms"] = round(total["latency_ms"] / requests, 1)
        total["cache_hit_rate"] = round(total["cached_tokens"] / max(1, total["prompt_tokens"]), 4)
        for group in (by_day, by_key, by_account, by_model):
            for bucket in group.values():
                bucket["avg_latency_ms"] = round(bucket["latency_ms"] / max(1, int(bucket["requests"])), 1)
        return {
            "total": total,
            "by_day": by_day,
            "by_key": by_key,
            "by_account": by_account,
            "by_model": by_model,
            "retention_days": self.retention_days,
            "recent": self.recent(50),
        }
