"""后台调度：令牌保活、余额刷新、签到、会话清理、落盘。

一个线程、30 秒一跳、没有 cron 依赖。两个参考实现都在这里踩过，所以规则写死：

* **令牌保活前置 6 小时**。旧版只在前 2 小时续期，结果账号 92% 的时间处在
  "马上过期"的空档里，上游一抖就整批掉线。也不能提前太早：刷新令牌是一次性的，
  每刷一次都是一次风险，所以"快到期才刷"比"提前 48 小时反复刷"安全得多。
* **账号之间必须错峰**。整点把全池的签到请求一起打出去 = 同一出口 IP 上的
  一次流量尖峰，那正是风控在找的形状。每个账号之间随机停顿 1~3 秒。
* **定时任务用"到点且没跑过"判断，不用 ``HH:MM == now``**。后者在跳秒、闹钟
  漂移、进程重启后必然漏跑一整天。这里比较 ``now >= 目标 && now - 目标 <= grace``。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .log import get_logger
from . import util

log = get_logger("scheduler")

TICK = 30.0
KEEPALIVE_AHEAD = 6 * 3600  # 令牌剩余寿命低于这个就续期
GRACE = 3 * 3600  # 定时任务的容错窗口：错过 3 小时内的任务补跑
SPREAD_MIN = 1.0  # 账号之间停顿时长（秒）
SPREAD_MAX = 3.0


def _spread() -> None:
    """账号之间随机停一下：把"批量任务"打散成人手滑动的形状。"""
    time.sleep(random.uniform(SPREAD_MIN, SPREAD_MAX))


class Job:
    def __init__(self, name: str, func: Callable[[], Any], interval: float = 0.0, at_hour: Optional[int] = None) -> None:
        self.name = name
        self.func = func
        self.interval = float(interval)
        self.at_hour = at_hour
        self.last_run = 0.0
        self.last_result = ""
        self.runs = 0
        self.errors = 0

    def due(self, now: float) -> bool:
        if self.at_hour is not None:
            target = _hour_mark(now, self.at_hour)
            return now >= target and (now - target) <= GRACE and self.last_run < target
        if self.interval <= 0:
            return False
        return (now - self.last_run) >= self.interval

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "interval": self.interval,
            "at_hour": self.at_hour,
            "last_run": int(self.last_run) if self.last_run else 0,
            "runs": self.runs,
            "errors": self.errors,
            "last_result": self.last_result,
        }


def _hour_mark(now: float, hour: int) -> float:
    """今天/昨天那个整点的时间戳（取不晚于 now 的最近一次）。"""
    local = time.localtime(now)
    mark = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, 0, 0, 0, 0, -1))
    if mark > now:
        mark -= 86400
    return mark


class Scheduler:
    def __init__(self, service: Any, cfg: Any) -> None:
        self.service = service
        self.cfg = cfg
        self.jobs: Dict[str, Job] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._register()

    # -- 任务表 -------------------------------------------------------------
    def _schedule_cfg(self) -> Dict[str, Any]:
        value = self.cfg.get("schedule") if self.cfg else {}
        return value if isinstance(value, dict) else {}

    def _register(self) -> None:
        schedule = self._schedule_cfg()
        interval_minutes = int(schedule.get("keepalive_minutes", 20) or 20)
        balance_minutes = int(schedule.get("balance_minutes", 180) or 180)
        checkin_hour = int(schedule.get("checkin_hour", 9) or 9)
        # enabled 开关必须真的生效：关掉的任务不进任务表，也就不占一次 tick 的等待
        if schedule.get("token_refresh_enabled", True):
            self.add(Job("keepalive", self.keepalive, interval=interval_minutes * 60))
        if schedule.get("balance_refresh_enabled", True):
            self.add(Job("balance", self.refresh_balances, interval=balance_minutes * 60))
        if schedule.get("checkin_enabled", True):
            self.add(Job("checkin", self.checkin, at_hour=checkin_hour))
        self.add(Job("housekeeping", self.housekeeping, interval=300))
        self.add(Job("flush", self.flush, interval=60))

    def add(self, job: Job) -> None:
        self.jobs[job.name] = job

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        log.info("调度器启动，任务：%s", ", ".join(self.jobs))

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("调度循环异常")
            self._stop.wait(TICK)

    # -- 执行 ---------------------------------------------------------------
    def run_once(self) -> Dict[str, str]:
        now = time.time()
        results: Dict[str, str] = {}
        for job in list(self.jobs.values()):
            if not job.due(now):
                continue
            job.last_run = now
            job.runs += 1
            try:
                result = job.func()
                job.last_result = str(result)[:200]
            except Exception as exc:
                job.errors += 1
                job.last_result = f"error: {exc}"[:200]
                log.warning("任务 %s 失败: %s", job.name, exc)
            results[job.name] = job.last_result
        return results

    def trigger(self, name: str) -> bool:
        job = self.jobs.get(name)
        if job is None:
            return False
        job.last_run = time.time()
        job.runs += 1
        try:
            job.last_result = str(job.func())[:200]
            return True
        except Exception as exc:
            job.errors += 1
            job.last_result = f"error: {exc}"[:200]
            log.warning("任务 %s 失败: %s", name, exc)
            return False

    def snapshot(self) -> Dict[str, Any]:
        return {
            "running": self._thread is not None,
            "jobs": [job.as_dict() for job in self.jobs.values()],
        }

    # -- 具体任务 -----------------------------------------------------------
    def keepalive(self) -> str:
        """把 6 小时内要过期的令牌续掉。刷新令牌是一次性的，所以逐个来、且要错峰。"""
        now = time.time()
        renewed = 0
        failed = 0
        seen = 0
        for account in self.service.store.list_accounts():
            if not account.enabled or not account.refresh_token:
                continue
            expires = float(account.expires_at or 0)
            # expires == 0 表示"到期时间未知"（静态密钥就是这种）。未知时续一次是划算的：
            # 刷新响应会带回真实 expiresIn，之后它就和别的账号一样被 KEEPALIVE_AHEAD 规则管住。
            if expires and (expires - now) > KEEPALIVE_AHEAD:
                continue
            if seen:
                _spread()
            seen += 1
            try:
                self.service.auth.refresh(account, force=True)
                renewed += 1
            except Exception as exc:
                failed += 1
                log.warning("保活失败 %s: %s", account.label, exc)
                if getattr(exc, "dead", False):
                    self.service.pool.note_auth_dead(account, str(exc))
        return f"renewed={renewed} failed={failed}"

    def refresh_balances(self) -> str:
        from .billing import Billing

        billing = Billing(self.service.client, self.cfg)
        updated = 0
        seen = 0
        for account in self.service.store.list_accounts():
            if not account.enabled or not account.access_token:
                continue
            if seen:
                _spread()
            seen += 1
            value = billing.credits(account, account.realm)
            if value is None:
                continue
            account.credit = value
            account.credit_at = util.now()
            self.service.pool.revive(account, balance_changed=True)
            updated += 1
        if updated:
            self.service.store.save_accounts()
        return f"updated={updated}"

    def checkin(self) -> str:
        """每日签到。默认只签 CN 域——global 域没有这个端点，打过去只是白费请求。"""
        from .billing import Billing

        billing = Billing(self.service.client, self.cfg)
        realms = self._schedule_cfg().get("checkin_realms") or ["cn"]
        done = 0
        seen = 0
        for realm in realms:
            for account in self.service.store.list_accounts(realm):
                if not account.enabled or not account.access_token:
                    continue
                if seen:
                    _spread()
                seen += 1
                ok, _message = billing.checkin(account, realm)
                done += 1 if ok else 0
        return f"checkin={done}"

    def housekeeping(self) -> str:
        self.service.affinity.gc()
        removed = self.service.ledger.prune() if hasattr(self.service.ledger, "prune") else 0
        return f"affinity={self.service.affinity.stats().get('size', 0)} pruned={removed}"

    def flush(self) -> str:
        self.service.pool.flush()
        self.service.store.flush_keys()
        return "flushed"
