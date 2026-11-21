# -*- coding: utf-8 -*-
"""进程内后台任务管理（Web 异步执行用）。

本地工作台不引入 Celery / Redis / 数据库：一个带锁的注册表 + 守护线程即可
支撑「提交 → 后台执行 → 轮询进度 → 取回结果」。任务结果保存在内存中，
服务重启后不恢复；这对本地单次演示/批处理是足够的，也避免额外依赖。

约定
----
- ``fn(progress)`` 在后台线程执行，``progress(current, total, message)`` 可
  被并发调用（实现内部加锁），回调异常只记日志、不影响任务结果；
- 任务返回任意可 JSON 化的 dict（供页面渲染），异常统一转为 ``failed``；
- ``snapshot()`` 返回纯 dict 快照，绝不把内部可变对象暴露给请求线程。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

LOGGER = logging.getLogger("etl")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
ACTIVE = (QUEUED, RUNNING)


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = QUEUED
    message: str = "已提交，等待后台线程启动"
    current: int = 0
    total: int = 1
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Any = None
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return round(max(0.0, end - (self.started_at or self.created_at)), 2)

    def progress(self) -> float:
        if self.status == DONE:
            return 1.0
        if not self.total:
            return 0.0
        return round(min(1.0, max(0.0, self.current / self.total)), 4)

    def to_dict(self, include_result: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "current": self.current,
            "total": self.total,
            "progress": self.progress(),
            "elapsed": self.elapsed(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "meta": dict(self.meta),
        }
        if include_result:
            out["result"] = self.result
        return out


class JobManager:
    """线程安全的后台任务注册表。"""

    def __init__(self, max_jobs: int = 80) -> None:
        self._lock = threading.Lock()
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._max_jobs = max(10, int(max_jobs))

    def submit(
        self,
        kind: str,
        label: str,
        fn: Callable[[Callable[[int, int, str], None]], Any],
        total: int = 1,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        job_id = "job_" + uuid.uuid4().hex[:12]
        job = Job(
            id=job_id, kind=kind, label=label,
            total=max(1, int(total or 1)),
            meta=dict(meta or {}),
        )
        with self._lock:
            self._jobs[job_id] = job
            self._prune_locked()
        thread = threading.Thread(
            target=self._run, args=(job_id, fn),
            name=f"dc-{kind}-{job_id}", daemon=True,
        )
        thread.start()
        LOGGER.info("后台任务已提交 id=%s kind=%s label=%s", job_id, kind, label)
        return job_id

    def _prune_locked(self) -> None:
        while len(self._jobs) > self._max_jobs:
            old_id, old = self._jobs.popitem(last=False)
            if old.status in ACTIVE:
                LOGGER.warning("丢弃仍在运行的过期任务记录 %s", old_id)

    def _run(self, job_id: str, fn: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = RUNNING
            job.started_at = time.time()
            job.message = "后台执行中"
        if job is None:
            return

        def progress(current: int, total: int, message: str) -> None:
            with self._lock:
                cur = self._jobs.get(job_id)
                if cur is None or cur.status not in ACTIVE:
                    return
                cur.current = max(0, int(current))
                cur.total = max(1, int(total or cur.total))
                cur.message = str(message or "后台执行中")

        try:
            result = fn(progress)
        except Exception as exc:  # noqa: BLE001 - 后台异常必须转成可读状态
            LOGGER.exception("后台任务失败 id=%s：%s", job_id, exc)
            with self._lock:
                cur = self._jobs.get(job_id)
                if cur is not None:
                    cur.status = FAILED
                    cur.error = f"{type(exc).__name__}: {exc}"
                    cur.message = "执行失败"
                    cur.finished_at = time.time()
        else:
            with self._lock:
                cur = self._jobs.get(job_id)
                if cur is not None:
                    cur.status = DONE
                    cur.current = cur.total
                    cur.message = "执行完成"
                    cur.result = result
                    cur.finished_at = time.time()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str, include_result: bool = False) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict(include_result=include_result) if job else None

    def result(self, job_id: str) -> Any:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.result if job is not None else None

    def list_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(reversed(list(self._jobs.values())))[:max(1, int(limit))]
            return [j.to_dict() for j in items]

    def wait(self, job_id: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """测试/脚本用：阻塞等待任务结束，返回最终快照。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            snap = self.snapshot(job_id)
            if snap is None:
                return None
            if snap["status"] not in ACTIVE:
                return snap
            time.sleep(0.02)
        return self.snapshot(job_id)


_MANAGER: Optional[JobManager] = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> JobManager:
    """全局单例：Flask 与标准库两条服务端路径共享同一批任务。"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = JobManager()
        return _MANAGER


__all__ = [
    "Job", "JobManager", "get_manager",
    "QUEUED", "RUNNING", "DONE", "FAILED", "ACTIVE",
]
