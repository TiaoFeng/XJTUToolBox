import os
import threading
import time
import unittest
from unittest.mock import patch

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


class RunGuardEndedThread(ProcessThread):
    """run() 先发出指定结束信号（canceled/hasFinished）再抛出漏网异常。"""

    def __init__(self, end=None):
        super().__init__()
        self.end = end

    def run(self):
        if self.end == "canceled":
            self.canceled.emit()
        elif self.end == "hasFinished":
            self.hasFinished.emit()
        raise ValueError("after-end")


class RunGuardUnhookThread(ProcessThread):
    """run() 先移除 canceled 上的所有连接（含兜底标记）再抛出漏网异常。"""

    def run(self):
        self.canceled.disconnect()
        raise ValueError("unhooked")


class RunGuardErrorThenRaiseThread(ProcessThread):
    """run() 先发出 error（随后可再发 canceled），再抛出漏网异常。"""

    def __init__(self, also_cancel=False):
        super().__init__()
        self.also_cancel = also_cancel

    def run(self):
        self.error.emit("操作失败", "业务已上报")
        if self.also_cancel:
            self.canceled.emit()
        raise ValueError("after-error")


class ReusableRunGuardThread(ProcessThread):
    """可复用线程：hang=True 时挂起等待强杀，否则等待 release 后正常结束。"""

    def __init__(self, hang=True):
        super().__init__()
        self.hang = hang
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self):
        self.entered.set()  # 包装器已连接标记，此时才是可靠的观察点
        if self.hang:
            while True:
                self.msleep(10)
        self.release.wait(5)
        self.hasFinished.emit()


def make_guarded_thread(error=None):
    """构造 run() 抛异常的线程，并记录 error/canceled/hasFinished 事件。"""
    thread = RunGuardThread(error)
    events = []
    thread.error.connect(lambda title, detail: events.append(("error", title, detail)))
    thread.canceled.connect(lambda: events.append(("canceled",)))
    thread.hasFinished.connect(lambda: events.append(("finished",)))
    return thread, events


class ProcessThreadRunGuardTest(unittest.TestCase):
    """ProcessThread 兜底子类 run() 中未捕获的异常。"""

    def _observe(self, thread):
        events = []
        thread.error.connect(lambda title, detail: events.append(("error", title, detail)))
        thread.canceled.connect(lambda: events.append(("canceled",)))
        return thread, events

    def test_unhandled_exception_reports_error_then_canceled(self):
        thread, events = make_guarded_thread(ValueError("boom"))

        thread.run()

        self.assertEqual(events, [("error", "操作失败", "boom"), ("canceled",)])
        self.assertFalse(thread.can_run)

    def test_empty_exception_message_falls_back_to_type_name(self):
        thread, events = make_guarded_thread(ValueError())

        thread.run()

        self.assertEqual(events, [("error", "操作失败", "ValueError"), ("canceled",)])

    def test_system_exit_is_not_swallowed(self):
        thread, events = make_guarded_thread(SystemExit(0))

        with self.assertRaises(SystemExit):
            thread.run()

        self.assertEqual(events, [])

    def test_started_thread_run_is_guarded_through_qt_dispatch(self):
        thread, events = make_guarded_thread(ValueError("boom"))

        thread.start()
        self.assertTrue(thread.wait(5000))
        APP.processEvents()  # 信号经主线程事件循环派发

        self.assertEqual(events, [("error", "操作失败", "boom"), ("canceled",)])

    def test_no_error_receiver_goes_to_global_hook(self):
        thread = RunGuardThread(ValueError("boom"))  # 不连接 error：异常应交给全局处理

        with patch("sys.excepthook") as hook:
            thread.run()

        self.assertFalse(thread.can_run)
        hook.assert_called_once()
        self.assertIs(hook.call_args.args[0], ValueError)

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

    def test_removed_mark_connection_does_not_escape_guard(self):
        """标记连接被外部移除时，finally 的清理不能把兜底变成新的异常源。"""
        thread = RunGuardUnhookThread()
        events = []
        thread.error.connect(lambda title, detail: events.append(("error", title, detail)))

        thread.run()  # 不应因清理失败抛出 TypeError

        self.assertFalse(thread.can_run)
        self.assertEqual(events, [("error", "操作失败", "unhooked")])


class ProcessThreadRunGuardTerminateTest(unittest.TestCase):
    """terminate() 强杀跳过 finally：标记连接不会跨 run 累积。"""

    def setUp(self):
        self.thread = ReusableRunGuardThread()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.thread.isRunning():
            self.thread.terminate()
            self.thread.wait(5000)

    def _start_and_wait_entered(self):
        self.thread.entered.clear()
        self.thread.start()
        self.assertTrue(self.thread.entered.wait(5000))

    def _assert_single_mark(self):
        # 运行中包装器恰好连接一个标记；强杀残留若未清理，这里会变成 2、3
        self.assertEqual(self.thread.receivers(self.thread.canceled), 1)
        self.assertEqual(self.thread.receivers(self.thread.hasFinished), 1)

    def test_terminate_leftovers_do_not_accumulate(self):
        for _ in range(3):
            self._start_and_wait_entered()
            self._assert_single_mark()
            self.thread.terminate()
            self.assertTrue(self.thread.wait(5000))

    def test_normal_finish_after_terminate_leaves_no_marks(self):
        for _ in range(2):
            self._start_and_wait_entered()
            self._assert_single_mark()
            self.thread.terminate()
            self.assertTrue(self.thread.wait(5000))

        self.thread.hang = False
        self._start_and_wait_entered()
        self._assert_single_mark()
        self.thread.release.set()
        self.assertTrue(self.thread.wait(5000))
        APP.processEvents()  # 派发 hasFinished 与 QThread.finished

        self.assertEqual(self.thread.receivers(self.thread.canceled), 0)
        self.assertEqual(self.thread.receivers(self.thread.hasFinished), 0)
        self.assertEqual(self.thread.receivers(self.thread.error), 0)


class ProcessThreadRunGuardAlreadyEndedTest(unittest.TestCase):
    """run() 已发出结束信号后再抛异常：兜底不应重复或矛盾上报。"""

    def _observe(self, thread):
        events = []
        thread.error.connect(lambda title, detail: events.append(("error", title, detail)))
        thread.canceled.connect(lambda: events.append(("canceled",)))
        thread.hasFinished.connect(lambda: events.append(("finished",)))
        return thread, events

    def test_exception_after_canceled_reports_only_once(self):
        thread, events = self._observe(RunGuardEndedThread("canceled"))

        thread.run()

        self.assertEqual(events, [("canceled",)])
        self.assertFalse(thread.can_run)

    def test_exception_after_has_finished_does_not_report_failure(self):
        thread, events = self._observe(RunGuardEndedThread("hasFinished"))

        thread.run()

        self.assertEqual(events, [("finished",)])
        self.assertFalse(thread.can_run)

    def test_end_marker_is_synchronous_through_qt_dispatch(self):
        # 跨线程运行时，结束信号必须在兜底读取 reported 之前同步写入标记；
        # 否则 worker 线程读到的 reported 为空，会补发 error/canceled。
        thread, events = self._observe(RunGuardEndedThread("canceled"))

        thread.start()
        self.assertTrue(thread.wait(5000))
        APP.processEvents()

        self.assertEqual(events, [("canceled",)])

    def test_end_markers_are_disconnected_after_run(self):
        thread = RunGuardEndedThread("canceled")  # 不连接接收者，只看标记是否清理

        thread.run()

        self.assertEqual(thread.receivers(thread.canceled), 0)
        self.assertEqual(thread.receivers(thread.hasFinished), 0)
        self.assertEqual(thread.receivers(thread.error), 0)

    def test_exception_after_error_and_canceled_is_not_reported_again(self):
        thread, events = self._observe(RunGuardErrorThenRaiseThread(also_cancel=True))

        thread.run()

        self.assertEqual(events, [("error", "操作失败", "业务已上报"), ("canceled",)])
        self.assertFalse(thread.can_run)


class ProcessThreadRunGuardErrorReportedTest(unittest.TestCase):
    """run() 已发出 error 后再抛异常：不重复 error；无接收者时仍走全局处理。"""

    def test_exception_after_error_reports_error_once_and_canceled(self):
        thread = RunGuardErrorThenRaiseThread()
        events = []
        thread.error.connect(lambda title, detail: events.append(("error", title, detail)))
        thread.canceled.connect(lambda: events.append(("canceled",)))
        thread.hasFinished.connect(lambda: events.append(("finished",)))

        thread.run()

        self.assertEqual(events, [("error", "操作失败", "业务已上报"), ("canceled",)])
        self.assertFalse(thread.can_run)

    def test_exception_after_error_without_receivers_uses_global_hook(self):
        thread = RunGuardErrorThenRaiseThread()  # 不连接 error：内部标记不得被当成接收者

        with patch("sys.excepthook") as hook:
            thread.run()

        hook.assert_called_once()
        self.assertIs(hook.call_args.args[0], ValueError)
        self.assertFalse(thread.can_run)
        self.assertEqual(thread.receivers(thread.error), 0)

    def test_error_report_is_synchronous_through_qt_dispatch(self):
        thread = RunGuardErrorThenRaiseThread()
        events = []
        thread.error.connect(lambda title, detail: events.append(("error", title, detail)))
        thread.canceled.connect(lambda: events.append(("canceled",)))

        thread.start()
        self.assertTrue(thread.wait(5000))
        APP.processEvents()

        self.assertEqual(events, [("error", "操作失败", "业务已上报"), ("canceled",)])

    def test_marks_are_disconnected_after_error_reported_run(self):
        thread = RunGuardErrorThenRaiseThread()
        thread.error.connect(lambda *_: None)
        thread.canceled.connect(lambda: None)
        thread.hasFinished.connect(lambda: None)

        thread.run()

        # 只剩外部接收者，兜底标记均已断开
        self.assertEqual(thread.receivers(thread.error), 1)
        self.assertEqual(thread.receivers(thread.canceled), 1)
        self.assertEqual(thread.receivers(thread.hasFinished), 1)


class ProcessThreadRunGuardWidgetTest(ProcessWidgetTestBase):
    """run() 异常兜底触发后，挂载的 ProcessWidget 经 canceled 收尾且不重复上报。"""

    def test_guarded_crash_ends_widget_via_canceled(self):
        thread, _ = make_guarded_thread(ValueError("boom"))
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

    def test_exception_after_canceled_does_not_double_report_widget(self):
        thread = RunGuardEndedThread("canceled")
        thread.error.connect(lambda *_: None)  # 有接收者时兜底会补发 canceled，才能暴露重复上报
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

    def test_exception_after_has_finished_does_not_leave_widget_canceled(self):
        thread = RunGuardEndedThread("hasFinished")
        thread.error.connect(lambda *_: None)  # 有接收者时兜底才会补发 canceled，才能暴露矛盾上报
        widget = self.make_process_widget(thread, stoppable=True, hide_on_end=False)
        canceled, finished = [], []
        widget.canceled.connect(lambda: canceled.append(True))
        widget.finished.connect(lambda: finished.append(True))

        thread.start()
        self.assertTrue(thread.wait(5000))
        APP.processEvents()  # 交付 error/canceled 与 QThread.finished

        self.assertEqual(canceled, [])
        self.assertEqual(finished, [True])
        self.assertFalse(widget.timer.isActive())

    def test_exception_after_error_ends_widget_once_via_canceled(self):
        thread = RunGuardErrorThenRaiseThread()
        thread.error.connect(lambda *_: None)  # 有接收者时兜底才会走「只补 canceled」路径
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
