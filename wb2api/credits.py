"""余额的"钱"视角：资源包清单、到期分桶、变动台账。

上游把额度拆成几十个资源包，每包有自己的到期日。只记一个总数会有两个盲点：

1. **看起来还有钱，其实下周就没了** —— 到手的是"总量"，不是"什么时候过期"；
2. **数字跳变无法解释** —— 总量掉了 3000，是花了、还是有个包过期了？

所以这里做三件小事：把包解析成结构化清单（`parse_packages`）、按到期时间分桶
（`expiry_buckets`）、把每次刷新记进一条 append-only 台账（`CreditBook`）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from . import util
from .log import get_logger

log = get_logger("credits")

# 上游时间戳是 UTC+8 的墙上时间（不带时区后缀），漏了这个偏移会整体差 8 小时。
TZ_CN = timezone(timedelta(hours=8))

BUCKETS = (
    ("expired", "已过期", 0.0),
    ("d3", "3 天内", 3 * 86400),
    ("d7", "7 天内", 7 * 86400),
    ("d30", "30 天内", 30 * 86400),
    ("later", "30 天以上", float("inf")),
)


def parse_time(value: Any) -> float:
    """把上游的时间字段解析成 epoch 秒；解析不了返回 0。"""
    if isinstance(value, (int, float)):
        if value <= 0:
            return 0.0
        return util.normalize_epoch(float(value))
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(text[: len(fmt) + 6].strip(), fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=TZ_CN).timestamp()
    return 0.0


def _first_number(item: Dict[str, Any], keys: Iterable[str]) -> Optional[float]:
    for key in keys:
        if key in item:
            value = util.to_float(item.get(key))
            if value is not None:
                return value
    return None


def parse_packages(payload: Any) -> List[Dict[str, Any]]:
    """腾讯计费侧的资源包形状：``data.Response.Data.Accounts[]`` 每行一个包。

    字段名上游改过几版（`CapacityRemainPrecise` / `CycleCapacityRemain`），这里按
    "先精确后粗略"的顺序取；一个都取不到就跳过这行——**不猜**，猜出来的余额会
    让 `credit_floor` 和面板同时说谎。
    """
    node = payload
    for key in ("data", "Response", "Data"):
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return []
    accounts = node.get("Accounts") if isinstance(node, dict) else None
    if not isinstance(accounts, list):
        return []

    now = time.time()
    packages: List[Dict[str, Any]] = []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        remain = _first_number(
            item,
            ("CapacityRemainPrecise", "CycleCapacityRemainPrecise", "CapacityRemain", "CycleCapacityRemain"),
        )
        if remain is None:
            continue
        size = _first_number(item, ("CapacitySize", "Capacity", "CycleCapacitySize")) or 0.0
        expire_at = parse_time(item.get("CycleEndTime") or item.get("ExpiredTime") or item.get("ExpireTime"))
        start_at = parse_time(item.get("CycleStartTime") or item.get("StartTime"))
        status = item.get("Status")
        expired = expire_at > 0 and expire_at < now
        # Status 非 0 且有到期时间 = 已失效；只在到期时间缺失时靠 Status 兜底。
        if not expired and expire_at <= 0 and status not in (None, 0, "0"):
            expired = True
        packages.append(
            {
                "name": str(item.get("Name") or item.get("ProductName") or item.get("AccountName") or ""),
                "remain": round(remain, 6),
                "size": round(size, 6),
                "expire_at": expire_at,
                "start_at": start_at,
                "expired": bool(expired),
                "status": status,
                "source": str(item.get("GrantSource") or item.get("Source") or ""),
            }
        )
    packages.sort(key=lambda row: (row["expired"], row["expire_at"] or float("inf")))
    return packages


def expiry_buckets(packages: List[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, Any]:
    """把包按"多久之后过期"分桶，返回 ``{bucket: credits}`` 与最近的那个到期时间。"""
    moment = now or time.time()
    buckets = {key: 0.0 for key, _label, _limit in BUCKETS}
    soonest = 0.0
    for row in packages:
        remain = float(row.get("remain") or 0.0)
        if remain <= 0:
            continue
        expire_at = float(row.get("expire_at") or 0.0)
        if row.get("expired") or (expire_at and expire_at < moment):
            buckets["expired"] += remain
            continue
        if expire_at <= 0:
            buckets["later"] += remain
            continue
        left = expire_at - moment
        if not soonest or expire_at < soonest:
            soonest = expire_at
        for key, _label, limit in BUCKETS[1:]:
            if left <= limit:
                buckets[key] += remain
                break
    labels = {key: label for key, label, _limit in BUCKETS}
    return {
        "buckets": {key: round(value, 6) for key, value in buckets.items()},
        "labels": labels,
        "soonest_expire_at": soonest,
    }


def summarize(packages: List[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, Any]:
    """面板与台账都用这一份摘要，避免两处各算一遍然后对不上。"""
    info = expiry_buckets(packages, now)
    usable = sum(float(row.get("remain") or 0.0) for row in packages if not row.get("expired"))
    info.update(
        {
            "total": round(usable, 6),
            "package_count": len(packages),
            "expiring_soon": round(info["buckets"]["d3"] + info["buckets"]["d7"], 6),
            "packages": packages[:24],
        }
    )
    return info


class CreditBook:
    """余额变动台账：一行一次刷新，回答"钱去哪了"。

    只追加、按天分文件、读尾部。写入用 `util.append_jsonl`（单次 `os.write`），
    所以掉电最多丢最后一行，不会留半行。
    """

    def __init__(self, data_dir: str, retention_days: int = 30) -> None:
        self.dir = os.path.join(str(data_dir or "."), "credits")
        self.retention_days = int(retention_days or 30)
        self._lock = threading.Lock()

    def _path(self, day: str) -> str:
        return os.path.join(self.dir, "credit-%s.jsonl" % day)

    def record(self, uid: str, realm: str, summary: Dict[str, Any], note: str = "") -> Dict[str, Any]:
        moment = time.time()
        event = {
            "at": moment,
            "iso": util.iso(moment),
            "uid": uid,
            "realm": realm,
            "credits": summary.get("total"),
            "expiring_soon": summary.get("expiring_soon"),
            "buckets": summary.get("buckets") or {},
            "package_count": summary.get("package_count"),
            "note": note,
        }
        try:
            with self._lock:
                os.makedirs(self.dir, exist_ok=True)
                util.append_jsonl(self._path(datetime.fromtimestamp(moment, TZ_CN).strftime("%Y-%m-%d")), event)
        except OSError as exc:  # 磁盘满/只读：余额展示比台账重要，别抛出去
            log.warning("写余额台账失败: %s", exc)
        return event

    def history(self, uid: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for path in sorted(self._files()):
            rows.extend(util.tail_jsonl(path, 0))
        rows = [row for row in rows if isinstance(row, dict)]
        if uid:
            rows = [row for row in rows if str(row.get("uid") or "") == uid]
        rows.sort(key=lambda row: row.get("at") or 0)
        return rows[-int(limit or 100):]

    def delta(self, uid: str = "", days: int = 7) -> Dict[str, Any]:
        """最近一段时间的净变化与消耗，用来回答"账对不对"。"""
        since = time.time() - max(1, int(days)) * 86400
        rows = [row for row in self.history(uid, 10000) if (row.get("at") or 0) >= since]
        if not rows:
            return {"samples": 0, "delta": 0.0, "spent": 0.0, "since": since}
        first = util.to_float(rows[0].get("credits"))
        last = util.to_float(rows[-1].get("credits"))
        spent = 0.0
        for prev, cur in zip(rows, rows[1:]):
            before = util.to_float(prev.get("credits"))
            after = util.to_float(cur.get("credits"))
            if before is not None and after is not None and after < before:
                spent += before - after
        return {
            "samples": len(rows),
            "first": first,
            "last": last,
            "delta": round((last or 0.0) - (first or 0.0), 6),
            "spent": round(spent, 6),
            "since": since,
        }

    def prune(self, now: Optional[float] = None) -> int:
        cutoff = (now or time.time()) - self.retention_days * 86400
        removed = 0
        for path in self._files():
            try:
                day = os.path.basename(path).replace("credit-", "").replace(".jsonl", "")
                stamp = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ_CN).timestamp()
            except ValueError:
                continue
            if stamp < cutoff:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        return removed

    def _files(self) -> List[str]:
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        return [os.path.join(self.dir, name) for name in names if name.startswith("credit-") and name.endswith(".jsonl")]


def dump(payload: Any) -> str:
    """测试与日志用：把摘要压成一行 JSON。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
