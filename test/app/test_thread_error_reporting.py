import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

TEST_DOMAIN = "qt-ui"
TEST_REGRESSION = True

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from app.threads.ProcessWidget import ProcessThread
from app.threads.error_reporting import report_thread_error
from app.utils.mfa import MFACancelledError, MFAUnavailableError
from app.utils.qrcode_login import QRCodeLoginCancelledError, QRCodeLoginUnavailableError
from auth import ServerError

if QApplication.instance() is None:
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

APP = QApplication.instance() or QApplication([])


def _fake_thread():
    """只有统一错误上报所需的 tr / error / canceled 三个成员。"""
    thread = SimpleNamespace()
    thread.tr = lambda text: text
    thread.error = Mock()
    thread.canceled = Mock()
    return thread


class ReportThreadErrorTest(unittest.TestCase):
    def test_each_exception_maps_to_expected_title(self):
        cases = (
            # 二维码登录错误继承自 MFA 错误，必须命中更具体的标题
            (QRCodeLoginCancelledError("cancel"), "扫码登录已取消"),
            (QRCodeLoginUnavailableError("missing"), "登录问题"),
            (MFACancelledError("cancel"), "安全验证已取消"),
            (MFAUnavailableError("missing"), "登录问题"),
            (ServerError(500, "server"), "服务器错误"),
            (requests.ConnectionError("offline"), "无网络连接"),
            # Timeout 是 ConnectionError 的兄弟，走通用网络错误
            (requests.Timeout("slow"), "网络错误"),
            (RuntimeError("unknown"), "其他错误"),
        )
        for error, title in cases:
            with self.subTest(error=type(error).__name__):
                thread = _fake_thread()
                report_thread_error(thread, error)
                thread.error.emit.assert_called_once()
                self.assertEqual(thread.error.emit.call_args.args[0], title)
                thread.canceled.emit.assert_called_once()

    def test_server_error_102_notifies_only_given_account(self):
        """账户在任务中途被移除或切换时，MFA 提示仍应发给发起任务的账户。"""
        captured = Mock(MFASignal=Mock())
        other = Mock(MFASignal=Mock())
        thread = _fake_thread()

        report_thread_error(thread, ServerError(102, "mfa"), account=captured)

        captured.MFASignal.emit.assert_called_once_with(True)
        other.MFASignal.emit.assert_not_called()
        thread.canceled.emit.assert_called_once()

    def test_server_error_102_without_account_still_finishes(self):
        thread = _fake_thread()

        report_thread_error(thread, ServerError(102, "mfa"), account=None)

        self.assertEqual(thread.error.emit.call_args.args[0], "登录问题")
        thread.canceled.emit.assert_called_once()

    def test_process_thread_uses_own_account_snapshot(self):
        thread = ProcessThread()
        account = Mock(MFASignal=Mock())
        thread.account = account
        errors, canceled = [], []
        thread.error.connect(lambda title, detail: errors.append(title))
        thread.canceled.connect(lambda: canceled.append(True))

        thread.report_error(ServerError(102, "mfa"))

        self.assertEqual(errors, ["登录问题"])
        self.assertEqual(canceled, [True])
        account.MFASignal.emit.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
