"""进程内按批次串行化锁：发布与观测补传共用同一把批次锁。

为什么需要
----------
发布批次 = 读取全部原始观测 → 内存解析 → 写入解析结果；
补传观测 = 校验批次状态 → 插入原始观测。
两者若不互斥，会出现“补传已提交、发布却按旧快照出结果”的丢失窗口。
同一把批次锁保证两种操作的提交顺序唯一：

- 补传先拿到锁并提交 → 发布随后读到新记录，纳入解析与汇总；
- 发布先拿到锁并完成 → 补传随后复查状态发现已 PUBLISHED，返回 409。

锁按批次粒度创建，不同批次互不阻塞：一个批次正在解析物种名时，
其他批次的补传照常提交。

注意：锁是进程内的，需配合单进程部署（compose 默认单 uvicorn 进程）。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_registry_guard = threading.Lock()
_batch_locks: dict[int, threading.Lock] = {}


def _lock_for(batch_id: int) -> threading.Lock:
    with _registry_guard:
        return _batch_locks.setdefault(batch_id, threading.Lock())


@contextmanager
def batch_lock(batch_id: int) -> Iterator[None]:
    """同一批次的发布与补传互斥；不同批次并行不悖。"""
    lock = _lock_for(batch_id)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
