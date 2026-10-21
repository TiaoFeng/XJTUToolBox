import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

TEST_DOMAIN = "qt-ui"
TEST_REGRESSION = True

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from app.threads.AttendanceFlowThread import AttendanceFlowThread
from auth import ServerError

if QApplication.instance() is None:
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

APP = QApplication.instance() or QApplication([])


def _thread():
    account = SimpleNamespace(type="undergraduate", POSTGRADUATE="postgraduate")
    thread = AttendanceFlowThread(account, size=10, page=1)
    thread.setIndeterminate = Mock()
    thread.messageChanged = Mock()
    return thread


class AttendanceFlowRetryTest(unittest.TestCase):
    """考勤流水查询的重试分支依赖 ServerError / requests 的导入。"""

    def test_server_error_is_retried_when_auto_retry_enabled(self):
        thread = _thread()
        lookup = Mock()
        lookup.getFlowRecordWithPage.side_effect = [ServerError(500, "busy"), {"data": []}]

        with patch("app.threads.AttendanceFlowThread.Attendance", return_value=lookup), \
                patch("app.threads.AttendanceFlowThread.time.sleep") as sleep, \
                patch("app.threads.AttendanceFlowThread.cfg") as cfg:
            cfg.autoRetryAttendance.value = True
            result = thread.search(Mock())

        self.assertEqual(result, {"data": []})
        self.assertEqual(lookup.getFlowRecordWithPage.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertEqual(
            thread.messageChanged.emit.call_args.args[0],
            "查询考勤流水失败，正在重试...",
        )

    def test_server_error_is_raised_when_auto_retry_disabled(self):
        thread = _thread()
        lookup = Mock()
        lookup.getFlowRecordWithPage.side_effect = ServerError(500, "busy")

        with patch("app.threads.AttendanceFlowThread.Attendance", return_value=lookup), \
                patch("app.threads.AttendanceFlowThread.time.sleep") as sleep, \
                patch("app.threads.AttendanceFlowThread.cfg") as cfg:
            cfg.autoRetryAttendance.value = False
            with self.assertRaises(ServerError):
                thread.search(Mock())

        sleep.assert_not_called()
        self.assertEqual(lookup.getFlowRecordWithPage.call_count, 1)


if __name__ == "__main__":
    unittest.main()
