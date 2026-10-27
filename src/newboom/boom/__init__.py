"""BOOM v1.0 API surface — one router module per doc page.

Aggregated under a single ``router`` that ``newboom.api`` includes. Each
submodule owns one page of ``docs/boom.v1.0.docx`` and exposes a ``router``:

* ``home``           — 1.1  首页
* ``status``         — 2.x  状态页面
* ``sleep``          — 3.x  睡眠页面
* ``energy``         — 4.x  储能页面
* ``load``           — 5.x  负荷页面
* ``superrecovery``  — 6.x  超量恢复页面
* ``body_metrics``   — 7.x  身体指标页面
* ``calendar``       — 8.x  日历图表
* ``questionnaire``  — 9.x  问卷页面
* ``coach``          — 11.x BOOM_COACH
* ``upload``         — 10.x 蓝牙数据上传

Bluetooth uploads (doc §10) are scaffolded by ``upload`` and implemented by
:mod:`newboom.upload` (mounted at ``/api/upload``, returns ``ApiResponse``).
The §10 request shapes follow ``docs/new-interface.md``.
"""

from __future__ import annotations

from fastapi import APIRouter

from newboom.boom.body_metrics import router as body_metrics_router
from newboom.boom.calendar import router as calendar_router
from newboom.boom.coach import router as coach_router
from newboom.boom.energy import router as energy_router
from newboom.boom.home import router as home_router
from newboom.boom.load import router as load_router
from newboom.boom.questionnaire import router as questionnaire_router
from newboom.boom.sleep import router as sleep_router
from newboom.boom.status import router as status_router
from newboom.boom.superrecovery import router as superrecovery_router
from newboom.boom.upload import router as upload_router

router = APIRouter()

router.include_router(home_router)
router.include_router(status_router)
router.include_router(sleep_router)
router.include_router(energy_router)
router.include_router(load_router)
router.include_router(superrecovery_router)
router.include_router(body_metrics_router)
router.include_router(calendar_router)
router.include_router(questionnaire_router)
router.include_router(upload_router)
router.include_router(coach_router)

__all__ = ["router"]
