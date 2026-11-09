"""Usage ledger: append-only JSONL plus in-memory aggregates for the panel.

Why JSONL and not SQLite: the gateway must stay dependency-free and a single
process owns the file.  Records are one line each, so ``tail``/``grep`` work for
debugging, and rotation is a file rename instead of a VACUUM.

写入走独立线程：请求线程只把记录放进队列就返回。之前是在请求线程里
``open + write + fsync``，高并发时所有线程排队等磁盘，而且一个慢盘能把
TTFT 拖到秒级——这条路径本来就不该出现在请求的关键路径上。

单个日文件超过 ``max_bytes`` 就滚动成 ``usage-<day>.p001.jsonl`` 这样的分片：
一天上百万请求时会写几百 MB，不滚动的话重启要全量回放一遍，启动慢得像是挂了。

Aggregation is done in memory at startup by replaying the retained files, then
kept up to date incrementally.  Every view is bounded by ``max_days``.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from . import util
from .log import get_logger

log = get_logger("usage")

DEFAULT_MAX_BYTES = 64 * 1024 * 1024
WRITE_BATCH = 512
QUEUE_MAX = 20000


class UsageLedger:
    def __init__(
        self,
        data_dir: str,
        retention_days: int = 7,
        archive_enabled: bool = True,
        *,
        async_write: bool = True,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.dir = os.path.join(os.path.abspath(data_dir), "usage")
        util.ensure_dir(self.dir)
        self.retention_days = max(1, int(retention_days))
        self.archive_enabled = bool(archive_enabled)
        self.max_bytes = max(1024 * 1024, int(max_bytes or DEFAULT_MAX_BYTES))
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
        self._dropped = 0
        self._closed = False
        self._async = bool(async_write)
        self._queue: Optional[queue.Queue] = queue.Queue(maxsize=QUEUE_MAX) if self._async else None
        self._writer: Optional[threading.Thread] = None
        if self._queue is not None:
            self._writer = threading.Thread(target=self._write_loop, name="usage-writer", daemon=True)
            self._writer.start()

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

    def file_for(self, day: str, part: int = 0) -> str:
        if part <= 0:
            return os.path.join(self.dir, f"usage-{day}.jsonl")
        return os.path.join(self.dir, f"usage-{day}.p{part:03d}.jsonl")

    # -- ingest -------------------------------------------------------------
    def record(self, event: Dict[str, Any]) -> None:
        record = dict(event)
        record.setdefault("ts", util.now())
        record.setdefault("day", self._day(record["ts"]))
        with self._lock:
            # 按天只回放一次：否则同一天的文件会被重放第二遍，总量凭空翻倍。
            self._ensure_day(str(record["day"]))
            self._apply(record)
            self._recent.append(record)
            del self._recent[:-500]
        if self._queue is None:
            self._append_now(record)
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # 队列满了就不要把请求拖死：直接同步写一次，慢也比丢数据好，
            # 但连续丢才说明盘真的不行，那就记一笔计数让面板能看出来。
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning("usage queue full (%s dropped-ish), writing inline", self._dropped)
            self._append_now(record)

    def _append_now(self, record: Dict[str, Any]) -> None:
        path = self._rotated_path(str(record["day"]))
        try:
            util.append_jsonl(path, record)
        except OSError as exc:
            log.warning("usage write failed: %s", exc)

    def _current_part(self, day: str) -> "tuple[int, int]":
        """返回该天"还没写满"的分片号与它当前的字节数。"""
        part = 0
        while True:
            path = self.file_for(day, part)
            try:
                size = os.path.getsize(path)
            except OSError:
                return part, 0
            if size < self.max_bytes:
                return part, size
            part += 1

    def _rotated_path(self, day: str) -> str:
        """返回当前该写的分片路径（超过阈值就换下一个分片号）。"""
        return self.file_for(day, self._current_part(day)[0])

    def _write_loop(self) -> None:
        assert self._queue is not None
        buf: List[Dict[str, Any]] = []
        idle = 0.0
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if buf:
                    self._write_batch(buf)
                    buf = []
                if self._closed:
                    return
                idle += 0.5
                continue
            if item is None:  # 关闭信号
                if buf:
                    self._write_batch(buf)
                return
            idle = 0.0
            buf.append(item)
            # 攒够一批或队列已经空了就落盘：单请求场景等价于立即写，
            # 峰值时是一次 write 而不是上千次。
            if len(buf) >= WRITE_BATCH or self._queue.empty():
                self._write_batch(buf)
                buf = []

    def _write_batch(self, records: List[Dict[str, Any]]) -> None:
        by_day: Dict[str, List[Dict[str, Any]]] = {}
        for record in records:
            by_day.setdefault(str(record.get("day") or self._day()), []).append(record)
        for day, items in by_day.items():
            part, used = self._current_part(day)
            path = self.file_for(day, part)
            chunk: List[bytes] = []
            for item in items:
                blob = (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8")
                # 一批记录可能就跨过分片阈值：按行累加判断，不能让一个批次
                # 把单文件写成几百 MB（那样重启回放会慢到像挂了）。
                if used and used + len(blob) > self.max_bytes:
                    self._write_bytes(path, chunk)
                    chunk, used = [], 0
                    part += 1
                    path = self.file_for(day, part)
                chunk.append(blob)
                used += len(blob)
            self._write_bytes(path, chunk)

    @staticmethod
    def _write_bytes(path: str, chunks: List[bytes]) -> None:
        if not chunks:
            return
        payload = b"".join(chunks)
        try:
            with open(path, "ab") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            log.warning("usage batch write failed: %s", exc)

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
            for day in self._days_on_disk():
                self._ensure_day(day)
        log.info(
            "usage ledger loaded: %s requests, %s days",
            int(self._counters["total"]["requests"]),
            len(self._loaded_days),
        )

    def _replay_file(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
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

    def _ensure_day(self, day: str) -> None:
        """Replay one day's file exactly once (also when it appears mid-run)."""
        if day in self._loaded_days:
            return
        self._loaded_days.add(day)
        for path in self._files_on_disk(day):
            self._replay_file(path)

    @staticmethod
    def _day_of(name: str) -> str:
        """``usage-2026-11-09.p002.jsonl`` → ``2026-11-09``。"""
        if not (name.startswith("usage-") and name.endswith(".jsonl")):
            return ""
        stem = name[len("usage-") : -len(".jsonl")]
        if ".p" in stem:
            stem = stem.split(".p", 1)[0]
        return stem

    def _files_on_disk(self, day: str = "") -> List[str]:
        found = []
        try:
            for name in os.listdir(self.dir):
                name_day = self._day_of(name)
                if not name_day:
                    continue
                if day and name_day != day:
                    continue
                found.append(os.path.join(self.dir, name))
        except OSError:
            pass
        found.sort()
        return found

    def _days_on_disk(self) -> List[str]:
        days = []
        for path in self._files_on_disk():
            day = self._day_of(os.path.basename(path))
            if day and day not in days:
                days.append(day)
        return days

    def _purge_old_files(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for path in self._files_on_disk():
            day = self._day_of(os.path.basename(path))
            try:
                stamp = time.mktime(time.strptime(day, "%Y-%m-%d"))
            except ValueError:
                continue
            if stamp >= cutoff:
                continue
            try:
                os.unlink(path)
                log.info("usage file rotated out: %s", path)
            except OSError:
                pass

    # -- views --------------------------------------------------------------
    def prune(self) -> int:
        """删掉超过保留期的日文件，返回删除个数。"""
        before = len(self._files_on_disk())
        self._purge_old_files()
        return before - len(self._files_on_disk())

    def flush(self) -> None:
        """把队列里剩下的记录写完（关闭流程与测试用）。"""
        if self._queue is None:
            return
        deadline = time.time() + 5.0
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)  # 给写线程一点时间把最后一批落盘

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            try:
                self._queue.put_nowait(None)  # type: ignore[union-attr]
            except queue.Full:
                pass
            self._writer.join(timeout=5.0)
            self._writer = None
        self._queue = None

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
            dropped = self._dropped
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
            "max_bytes": self.max_bytes,
            "queue_backlog": self._queue.qsize() if self._queue is not None else 0,
            "inline_fallbacks": dropped,
            "recent": self.recent(50),
        }
