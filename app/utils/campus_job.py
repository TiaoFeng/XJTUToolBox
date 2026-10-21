"""Shared login + error handling for campus feature worker threads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.threads.ProcessWidget import ProcessThread
from app.threads.error_reporting import report_thread_error
from app.utils import accounts

# Distinguishes a failed/canceled job from a worker that legitimately returned None.
JOB_FAILED = object()


def run_campus_job(
        thread: ProcessThread,
        *,
        site_key: str,
        login_message: str,
        worker: Callable[[Any], Any],
        need_login: bool = True) -> Any:
    """Login a captured account into ``site_key`` (optional) and run ``worker``.

    The current account is snapshotted at start so a mid-job account switch cannot
    mix credentials. ``worker`` receives the site session (or ``None`` when
    ``need_login`` is false). Failure paths return ``JOB_FAILED``; a successful
    worker may still return ``None``.
    """
    thread.can_run = True
    account = accounts.current
    if need_login and account is None:
        thread.error.emit(thread.tr("未登录"), thread.tr("请先添加一个账户"))
        thread.canceled.emit()
        thread.can_run = False
        return JOB_FAILED

    try:
        session = None
        if need_login:
            session = account.session_manager.get_session(site_key)
            thread.setIndeterminate.emit(True)
            thread.messageChanged.emit(login_message)
            session.ensure_login(
                account.username,
                account.password,
                account=account,
                mfa_provider=account.session_manager.mfa_provider,
            )
            if not thread.can_run:
                thread.canceled.emit()
                return JOB_FAILED
        result = worker(session)
        if not thread.can_run:
            thread.canceled.emit()
            return JOB_FAILED
        return result
    except Exception as e:
        report_thread_error(thread, e, account=account)
    thread.can_run = False
    return JOB_FAILED
