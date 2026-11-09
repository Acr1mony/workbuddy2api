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
from . import identity, util

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

    def _duration(self, path: tuple, default: float) -> float:
        """配置里的时长写成 ``"60s"`` 这种人话，读的时候换成秒。"""
        raw = self.cfg.get(".".join(path), None) if self.cfg else None
        if raw is None or raw == "":
            return default
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            return float(util.parse_duration(str(raw)))
        except (TypeError, ValueError):
            return default

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
        if schedule.get("report_enabled", True):
            self.add(Job("report", self.report_activity, at_hour=int(schedule.get("report_hour", 10) or 10)))
        if schedule.get("quota_probe_enabled", True):
            self.add(Job("quota_probe", self.quota_probe,
                         interval=int(schedule.get("quota_probe_minutes", 10) or 10) * 60))
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
        updated = 0
        seen = 0
        for account in self.service.store.list_accounts():
            if not account.enabled or not account.access_token:
                continue
            if seen:
                _spread()
            seen += 1
            # refresh_credit 顺手把"哪个包快过期"记下来并写一条变动台账——
            # 只看总额的版本会在最大那个包到期的早上突然变成 0，而面板上什么都查不到。
            info = self.service.refresh_credit(account, account.realm, note="schedule")
            if not info.get("ok"):
                continue
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

    def report_activity(self) -> str:
        """活跃上报：每个号每天一条，点亮连登天数 / 解锁 first_buddy。

        企业号显式跳过：上报接口对它照样回 200，但 growth 体系对个人号之外一律
        403（`growth system is only available for personal users`），也就是零收益。
        这种"静默无效"最容易被当成"已经在做了"，所以宁可少发一次请求。
        """
        from .billing import Billing

        billing = Billing(self.service.client, self.cfg)
        realms = self._schedule_cfg().get("report_realms") or ["cn", "global"]
        realms = [identity.normalize_realm(item) for item in realms]
        done = 0
        skipped = 0
        seen = 0
        for account in self.service.store.list_accounts():
            if not account.enabled or not account.access_token:
                continue
            realm = identity.normalize_realm(account.realm)
            if realm not in realms:
                continue
            if getattr(account, "enterprise_id", "") or getattr(account, "product", "") == "enterprise":
                skipped += 1
                continue
            if seen:
                _spread()
            seen += 1
            ok = billing.report(account, realm)
            if ok:
                done += 1
                extra = getattr(account, "extra", None)
                if isinstance(extra, dict):
                    extra["reported_at"] = util.now()
            if seen % 20 == 0:
                self.service.store.save_accounts()
        if done:
            self.service.store.save_accounts()
        return f"reported={done} skipped_enterprise={skipped}"

    def quota_probe(self) -> str:
        """配额耗尽后的验证探针：到期后主动打一发最小请求，而不是等真实流量去撞。

        402 带重置时间时账号会被挂到那一刻（甚至是次日 04:00）。到点之后"额度真的回来了"
        没有任何本地证据——真实流量的第一次尝试会拿一个很可能失败的请求去试，代价是
        用户看到一次失败、账号多记一次失败计数。这里花 16 个 token 先探一次，成功就
        按"真跑通了"清窗口（note_success 是解禁的唯一证据）。
        """
        from .probe import probe_account

        now = time.time()
        grace = self._duration(("pool", "quota_probe_grace"), 60.0)
        retry = self._duration(("pool", "quota_probe_retry"), 6 * 3600)
        probed = 0
        recovered = 0
        seen = 0
        for account in self.service.store.list_accounts():
            if not account.enabled or not account.access_token:
                continue
            until = float(getattr(account, "balance_until", 0) or 0)
            if not until:
                account.extra.pop("quota_probe_at", None)
                continue
            due = float(account.extra.get("quota_probe_at", 0) or 0)
            if not due:
                # 第一次看到这个号挂起：约定在解禁之后 grace 秒探一次。
                account.extra["quota_probe_at"] = until + grace
                continue
            if due > now:
                continue
            if seen:
                _spread()
            seen += 1
            probed += 1
            ok, detail = probe_account(self.service, account)
            if ok:
                self.service.pool.note_success(account)
                account.extra.pop("quota_probe_at", None)
                recovered += 1
                log.info("配额已恢复 %s（%s）", account.label, detail.get("model", ""))
            else:
                # 还没恢复：退到 6 小时后再探，别在重置点附近连打。
                account.extra["quota_probe_at"] = now + retry
                log.info("配额仍未恢复 %s: %s", account.label, str(detail.get("error", ""))[:120])
        if seen:
            self.service.store.save_accounts()
        return f"probed={probed} recovered={recovered}"

    def housekeeping(self) -> str:
        self.service.affinity.gc()
        removed = self.service.ledger.prune() if hasattr(self.service.ledger, "prune") else 0
        credit_rows = 0
        if hasattr(self.service, "credit_book"):
            credit_rows = self.service.credit_book.prune()
        aff = self.service.affinity.stats()
        return (
            f"affinity={aff.get('sessions', 0)} rotated={aff.get('rotations', 0)} "
            f"pruned={removed} credits_pruned={credit_rows}"
        )

    def flush(self) -> str:
        self.service.pool.flush()
        self.service.store.flush_keys()
        return "flushed"
