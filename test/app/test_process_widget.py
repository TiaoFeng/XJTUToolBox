import os
import threading
import time
import unittest

TEST_DOMAIN = "qt-ui"
TEST_REGRESSION = True

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_STATE_HOME", "/tmp/xjtu-test-state")
os.environ.setdefault("XDG_CONFIG_HOME", "/tmp/xjtu-test-config")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from app.threads.ProcessWidget import ProcessThread, ProcessWidget

if QApplication.instance() is None:
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

APP = QApplication.instance() or QApplication([])


class ProcessWidgetTestBase(unittest.TestCase):
    """ProcessWidget 测试公共基类：统一负责 widget/thread 的创建与清理。"""

    def _teardown(self, widget, thread):
        """清理期间同时持有 widget 和 thread，防止 GC 先删掉子 QTimer。"""
        widget.timer.stop()
        if thread.isRunning():
            thread.quit()
            thread.wait(5000)
        widget.close()

    def make_process_widget(self, thread, stoppable=True, hide_on_end=True):
        widget = ProcessWidget(thread, stoppable=stoppable, hide_on_end=hide_on_end)
        # 绑定方法持有 self，参数持有 widget/thread，保证清理执行期间对象存活
        self.addCleanup(self._teardown, widget, thread)
        return widget


class SilentExitThread(ProcessThread):
    """模拟线程异常退出：run() 直接返回，不发任何结束信号。"""

    def run(self):
        return


class ProcessWidgetSilentExitTest(ProcessWidgetTestBase):
    def make_widget(self):
        thread = SilentExitThread()
        widget = self.make_process_widget(thread, stoppable=True, hide_on_end=True)
        canceled, finished = [], []
        widget.canceled.connect(lambda: canceled.append(True))
        widget.finished.connect(lambda: finished.append(True))
        return widget, thread, canceled, finished

    def test_silent_exit_reports_and_stops_timer(self):
        widget, thread, canceled, finished = self.make_widget()
        thread.start()
        self.assertTrue(thread.wait(5000))
        APP.processEvents()  # 交付 started 与 QThread.finished

        self.assertEqual(canceled, [True])
        self.assertEqual(finished, [])
        self.assertFalse(widget.timer.isActive())

        widget.checkProcess()  # 再检查也不重复上报
        self.assertEqual(canceled, [True])

    def test_has_finished_does_not_report_canceled(self):
        widget, thread, canceled, finished = self.make_widget()
        widget.onThreadStart()
        thread.hasFinished.emit()  # 同线程直连 -> onFinished
        widget.onThreadExited()  # 模拟 QThread.finished
        self.assertEqual(finished, [True])
        self.assertEqual(canceled, [])
        self.assertFalse(widget.timer.isActive())

    def test_thread_canceled_is_reported_once(self):
        widget, thread, canceled, finished = self.make_widget()
        widget.onThreadStart()
        thread.canceled.emit()  # 同线程直连 -> onStopped
        widget.onThreadExited()
        self.assertEqual(canceled, [True])
        self.assertEqual(finished, [])

    def test_user_cancel_path_still_reports_once(self):
        widget, _, canceled, _ = self.make_widget()
        widget.onThreadStart()
        widget.onCancelButtonClicked()
        widget.onThreadExited()
        self.assertEqual(len(canceled), 1)
        widget.onThreadExited()
        self.assertEqual(len(canceled), 1)


class HasFinishedThread(ProcessThread):
    """release 被 set 后才发 hasFinished 并退出。"""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def run(self):
        self.release.wait(5)
        self.hasFinished.emit()


class ProcessWidgetFinishRaceTest(ProcessWidgetTestBase):
    def test_check_process_does_not_race_queued_finish(self):
        thread = HasFinishedThread()
        widget = self.make_process_widget(thread, stoppable=True, hide_on_end=False)

        canceled, finished = [], []
        widget.canceled.connect(lambda: canceled.append(True))
        widget.finished.connect(lambda: finished.append(True))

        thread.start()
        deadline = time.time() + 5
        while not widget.timer.isActive() and time.time() < deadline:
            APP.processEvents()  # 等 started 送达，启动监控定时器

        thread.release.set()
        self.assertTrue(thread.wait(5000))
        # hasFinished 与 QThread.finished 均已从 worker 发出，但还没派发到主线程
        widget.checkProcess()  # 若按 isRunning() 轮询判断，会误判为异常退出并补发 canceled
        self.assertEqual(canceled, [])

        APP.processEvents()  # 派发排队的结束信号
        self.assertEqual(canceled, [])
        self.assertEqual(finished, [True])
        self.assertFalse(widget.timer.isActive())


class RunGuardThread(ProcessThread):
    """run() 抛出漏网之鱼，用于验证base类兜底。"""

    def __init__(self, error=None):
        super().__init__()
        self.error_to_raise = error if error is not None else ValueError("boom")

    def run(self):
        raise self.error_to_raise


class RunGuardChildThread(RunGuardThread):
    """不重定义 run：继承的包装仍然生效。"""


class RunGuardGrandChildThread(RunGuardChildThread):
    """重定义 run：应被重新包装，且只上报一次。"""

    def run(self):
        raise ValueError("grand-child")


class ProcessThreadRunGuardTest(unittest.TestCase):
    """ProcessThread 兜底子类 run() 中未捕获的异常。"""

    @staticmethod
    def _guard_thread(error=None):
        thread = RunGuardThread(error)
        events = []
        thread.error.connect(lambda title, detail: events.append(("error", title, detail)))
        thread.canceled.connect(lambda: events.append(("canceled",)))
        thread.hasFinished.connect(lambda: events.append(("finished",)))
        return thread, events

    def _observe(self, thread):
        events = []
        thread.error.connect(lambda title, detail: events.append(("error", title, detail)))
        thread.canceled.connect(lambda: events.append(("canceled",)))
        return thread, events

    def test_unhandled_exception_reports_error_then_canceled(self):
        thread, events = self._guard_thread(ValueError("boom"))

        thread.run()

        self.assertEqual(events, [("error", "操作失败", "boom"), ("canceled",)])
        self.assertFalse(thread.can_run)

    def test_empty_exception_message_falls_back_to_type_name(self):
        thread, events = self._guard_thread(ValueError())

        thread.run()

        self.assertEqual(events, [("error", "操作失败", "ValueError"), ("canceled",)])

    def test_system_exit_is_not_swallowed(self):
        thread, events = self._guard_thread(SystemExit(0))

        with self.assertRaises(SystemExit):
            thread.run()

        self.assertEqual(events, [])

    def test_started_thread_run_is_guarded_through_qt_dispatch(self):
        thread, events = self._guard_thread(ValueError("boom"))

        thread.start()
        self.assertTrue(thread.wait(5000))
        APP.processEvents()  # 信号经主线程事件循环派发

        self.assertEqual(events, [("error", "操作失败", "boom"), ("canceled",)])

    def test_no_error_receiver_reraises_to_global_hook(self):
        thread = RunGuardThread(ValueError("boom"))  # 不连接 error：异常应冒泡交给全局处理

        with self.assertRaises(ValueError):
            thread.run()

        self.assertFalse(thread.can_run)

    def test_inherited_run_is_still_guarded(self):
        thread, events = self._observe(RunGuardChildThread(ValueError("inherited")))

        thread.run()

        self.assertEqual(events, [("error", "操作失败", "inherited"), ("canceled",)])

    def test_receivers_api_available(self):
        thread = RunGuardThread()
        # 确保 receivers() 的可用性与语义正确：若移除该 API 时在此处报错
        self.assertEqual(thread.receivers(thread.error), 0)
        thread.error.connect(lambda *a: None)
        self.assertGreater(thread.receivers(thread.error), 0)

    def test_redefined_run_is_wrapped_exactly_once(self):
        thread, events = self._observe(RunGuardGrandChildThread())

        thread.run()

        self.assertEqual(events, [("error", "操作失败", "grand-child"), ("canceled",)])
        self.assertEqual(type(thread).run.__name__, "run")
        original = type(thread).run.__wrapped__
        self.assertFalse(getattr(original, "_process_thread_guarded", False))


class ProcessThreadRunGuardWidgetTest(ProcessWidgetTestBase):
    """run() 异常兜底触发后，挂载的 ProcessWidget 经 canceled 收尾且不重复上报。"""

    def test_guarded_crash_ends_widget_via_canceled(self):
        thread, _ = ProcessThreadRunGuardTest._guard_thread(ValueError("boom"))
        widget = self.make_process_widget(thread, stoppable=True, hide_on_end=False)
        canceled, finished = [], []
        widget.canceled.connect(lambda: canceled.append(True))
        widget.finished.connect(lambda: finished.append(True))

        thread.start()
        self.assertTrue(thread.wait(5000))
        APP.processEvents()  # 交付 error/canceled 与 QThread.finished

        self.assertEqual(canceled, [True])
        self.assertEqual(finished, [])
        self.assertFalse(widget.timer.isActive())


if __name__ == "__main__":
    unittest.main()
