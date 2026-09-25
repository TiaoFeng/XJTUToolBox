import os
import threading
import unittest
from unittest.mock import patch

TEST_DOMAIN = "qt-ui"
TEST_REGRESSION = True

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_STATE_HOME", "/tmp/xjtu-test-state")
os.environ.setdefault("XDG_CONFIG_HOME", "/tmp/xjtu-test-config")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

if QApplication.instance() is None:
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from qfluentwidgets import InfoBarPosition

from app.components.ProgressInfoBar import ProgressBarThread, ProgressInfoBar

APP = QApplication.instance() or QApplication([])


class SilentExitThread(ProgressBarThread):
    """run() 不发任何结束信号：模拟异常/静默退出；release 存在时先挂起。"""

    def __init__(self, release=None):
        super().__init__()
        self.entered = threading.Event()
        self.release = release

    def run(self):
        self.entered.set()
        if self.release is not None:
            self.release.wait(5)


class FinishingThread(ProgressBarThread):
    """正常结束：发 hasFinished 后返回。用于验证旧线程的 hasFinished 被断开。"""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self):
        self.entered.set()
        self.release.wait(5)
        self.hasFinished.emit()


class ProgressInfoBarTest(unittest.TestCase):
    def make_bar(self, thread):
        bar = ProgressInfoBar("标题", "内容", position=InfoBarPosition.NONE)
        events = []
        bar.canceled.connect(lambda: events.append("canceled"))
        bar.finished.connect(lambda: events.append("finished"))
        bar.connectToThread(thread)
        self.addCleanup(self._teardown, bar, thread)
        return bar, events

    @staticmethod
    def _teardown(bar, *threads):
        # bar.thread_ 一并纳入：重绑定用例里 make_bar 只传了旧线程，
        # 保证退出后才释放引用。
        targets = [t for t in dict.fromkeys((*threads, bar.thread_)) if t is not None]
        for thread in targets:
            release = getattr(thread, "release", None)
            if release is not None:
                release.set()
        for thread in targets:
            if thread.isRunning():
                thread.quit()
                thread.wait(5000)
        try:
            bar.timer.stop()
            bar.close()
        except RuntimeError:
            pass # close() -> deleteLater() 已销毁对象

    def assertTimerStopped(self, bar):
        try:
            active = bar.timer.isActive()
        except RuntimeError:
            active = False  # 对象已销毁，定时器必然不存在
        self.assertFalse(active)

    def wait_entered(self, thread):  # 保证 started 已入队后再 processEvents
        self.assertTrue(thread.entered.wait(5))

    # 原实现这里 events == []
    def test_silent_exit_reports_canceled_exactly_once(self):
        thread = SilentExitThread()
        bar, events = self.make_bar(thread)

        thread.start()
        self.assertTrue(thread.wait(5000))
        with patch("app.components.ProgressInfoBar.logger") as logger:
            APP.processEvents()

        self.assertEqual(events, ["canceled"])
        self.assertTrue(bar._saw_end)
        self.assertTimerStopped(bar)
        logger.warning.assert_called_once()
        self.assertIn("线程未发送结束信号", logger.warning.call_args[0][0])

    # 成功路径不变：只有 finished，且不会记录 warning
    def test_normal_finish_reports_finished_without_warning(self):
        thread = FinishingThread()
        thread.release.set()
        bar, events = self.make_bar(thread)

        thread.start()
        self.assertTrue(thread.wait(5000))
        with patch("app.components.ProgressInfoBar.logger") as logger:
            APP.processEvents()

        self.assertEqual(events, ["finished"])
        self.assertTrue(bar._saw_end)
        logger.warning.assert_not_called()

    # 结束判定不许放回 checkProcess
    def test_polling_does_not_decide_end_while_signals_pending(self):
        thread = FinishingThread()  # 先 clear，让 worker 挂在 run 里
        bar, events = self.make_bar(thread)

        thread.start()
        self.wait_entered(thread)
        APP.processEvents()  # 派发 started -> 定时器真的在跑
        self.assertTrue(bar.timer.isActive())

        thread.release.set()  # 线程退出，但结束信号仍在队列里
        self.assertTrue(thread.wait(5000))

        with patch("app.components.ProgressInfoBar.logger") as logger:
            bar.checkProcess()  # 轮询不得自行判定结束
        self.assertEqual(events, [])
        self.assertFalse(bar._saw_end)
        self.assertTimerStopped(bar)
        logger.warning.assert_not_called()

        APP.processEvents()  # hasFinished 先于 QThread.finished 派发
        self.assertEqual(events, ["finished"])
        self.assertTrue(bar._saw_end)

    # 点关闭后线程静默退出；重复 onThreadExited 不重复发
    def test_close_click_then_silent_exit_reports_once(self):
        release = threading.Event()
        thread = SilentExitThread(release)
        bar, events = self.make_bar(thread)

        thread.start()
        self.wait_entered(thread)
        APP.processEvents()
        bar.onCloseButtonClicked()  # stopped=True，closedSignal -> onStopSignal

        release.set()
        self.assertTrue(thread.wait(5000))
        APP.processEvents()
        self.assertEqual(events, ["canceled"])

        bar.onThreadExited()  # 二次调用（此时 sender() 为 None）
        bar.onThreadExited()
        self.assertEqual(events, ["canceled"])

    # 旧线程的任何信号都不再影响组件
    def test_rebinding_disconnects_old_thread(self):
        old = FinishingThread()  # 会发 hasFinished，才能覆盖断开缺口
        current = FinishingThread()
        bar, events = self.make_bar(old)
        bar.connectToThread(
            current
        )  # 不修（progressPause → progressPaused）的话这里 AttributeError

        old.release.set()
        old.start()
        self.assertTrue(old.wait(5000))
        APP.processEvents()

        self.assertEqual(events, [])
        self.assertFalse(bar._saw_end)
        self.assertFalse(bar.timer.isActive())
        self.assertIs(bar.thread_, current)


if __name__ == "__main__":
    unittest.main()
