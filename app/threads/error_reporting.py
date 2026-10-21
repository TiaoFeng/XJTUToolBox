"""子线程统一的失败上报。

后台任务在 ``run()`` 里把业务异常交给 :func:`report_thread_error`，由这里完成
「异常 → 用户可见文案」的映射，并按 ``docs/development/thread.md`` 的契约
先发出 ``error(title, message)``、再发出 ``canceled``。

放在这里而不是各线程里，是为了让登录、网络、服务器错误的提示只有一份实现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import requests

from auth import ServerError
from ..utils.account import request_mfa
from ..utils.log import logger
from ..utils.mfa import MFACancelledError, MFAUnavailableError
from ..utils.qrcode_login import QRCodeLoginCancelledError, QRCodeLoginUnavailableError

if TYPE_CHECKING:
    from ..utils.account import Account
    from .ProcessWidget import ProcessThread


def report_thread_error(
        thread: ProcessThread,
        error: Exception,
        account: Account | None = None) -> None:
    """把任务异常转换成用户提示，并结束本次任务。

    :param thread: 发起任务的线程，提供 ``tr``、``account``、``error``、``canceled``。
    :param error: 捕获到的异常对象。
    :param account: 需要接收 MFA 提示的账户。默认取 ``thread.account``，
        也就是线程在 ``run()`` 开始时快照的账户；这样任务中途移除或切换账户时，
        提示仍然发给发起任务的账户，而不是 ``accounts.current``。
    """
    if account is None:
        account = thread.account

    # 二维码登录错误同时继承自 MFA 错误，必须先匹配子类，否则提示会串到 MFA 分支
    if isinstance(error, QRCodeLoginCancelledError):
        logger.info("二维码登录已取消：%s", error)
        thread.error.emit(thread.tr("扫码登录已取消"), thread.tr("已取消扫码登录，本次操作未完成。"))
    elif isinstance(error, QRCodeLoginUnavailableError):
        logger.error("二维码登录交互不可用", exc_info=error)
        thread.error.emit(thread.tr("登录问题"), str(error))
    elif isinstance(error, MFACancelledError):
        logger.info("MFA 验证已取消：%s", error)
        thread.error.emit(thread.tr("安全验证已取消"), thread.tr("已取消安全验证，本次操作未完成。"))
    elif isinstance(error, MFAUnavailableError):
        logger.error("MFA 交互不可用", exc_info=error)
        thread.error.emit(thread.tr("登录问题"), str(error))
    elif isinstance(error, ServerError):
        logger.error("服务器错误", exc_info=error)
        if error.code == 102:
            # 102 表示服务器要求两步验证，这里只负责提醒，不重试本次任务
            thread.error.emit(
                thread.tr("登录问题"),
                thread.tr("需要进行两步验证，请前往账户界面，选择对应账户进行验证。"),
            )
            request_mfa(account)
        else:
            thread.error.emit(thread.tr("服务器错误"), error.message)
    elif isinstance(error, requests.ConnectionError):
        # ConnectionError 是 RequestException 的子类，必须放在前面
        logger.error("网络错误", exc_info=error)
        thread.error.emit(thread.tr("无网络连接"), thread.tr("请检查网络连接，然后重试。"))
    elif isinstance(error, requests.RequestException):
        logger.error("网络错误", exc_info=error)
        thread.error.emit(thread.tr("网络错误"), str(error))
    else:
        logger.error("其他错误", exc_info=error)
        thread.error.emit(thread.tr("其他错误"), str(error))

    thread.canceled.emit()
