import functools
import sys
import time

from PyQt5.QtWidgets import QFrame, QHBoxLayout
from PyQt5.QtCore import QThread, pyqtSignal, pyqtSlot, QTimer, Qt
from qfluentwidgets import ProgressBar, VBoxLayout, BodyLabel, PrimaryPushButton, IndeterminateProgressBar, \
    MessageBoxBase

from ..utils import logger


class ProcessWidget(QFrame):
    """一个框架，包含一个进度条和一个标签，用于让子线程方便的报告状态"""
    # 内部信号，用于通知进程停止
    stop = pyqtSignal()
    # 进程停止（错误的退出或被主动终止）
    canceled = pyqtSignal()
    # 进程成功完成
    finished = pyqtSignal()

    def __init__(self, thread: "ProcessThread", parent=None, stoppable=False, hide_on_end=True, backward_animation=True):
        super().__init__(parent)
        # 点击取消时的进度倒退动画
        self.backward_animation = backward_animation
        self._speed = 0
        self.thread_ = thread
        self.hide_on_end = hide_on_end
        # 初始化自身的组件
        self.vBoxLayout = VBoxLayout(self)
        self.messageFrame = QFrame(self)
        self.hBoxLayout = QHBoxLayout(self.messageFrame)
        self.vBoxLayout.addWidget(self.messageFrame)
        self.stoppable = stoppable

        self.label = BodyLabel(self)
        self.hBoxLayout.addWidget(self.label)
        self.stopButton = PrimaryPushButton(self.tr("取消"), self)
        self.hBoxLayout.addWidget(self.stopButton)
        # 是否已经要求子线程退出
        self.stopped = False
        # 是否已经观察到线程的结束信号
        self._saw_end = False
        # 是否为确定进度条
        self.isIndeterminate = True
        if not stoppable:
            self.stopButton.setVisible(False)
            self.stopButton.setEnabled(False)

        self.progressBar = ProgressBar(self)
        self.indeterminateProgressBar = IndeterminateProgressBar(self)
        self.vBoxLayout.addWidget(self.progressBar)
        self.vBoxLayout.addWidget(self.indeterminateProgressBar)
        self.indeterminateProgressBar.setVisible(False)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.checkProcess)

        # 在通知子线程退出后，最多 5 秒后强制退出（直接终止子线程）
        # 此设计是为了防止子线程在网络请求中长时间卡顿，导致无法响应退出请求。
        # 子线程可以通过发送信号更改这一时间长度
        self.thread_dead_time = 5
        # 父线程发起退出请求的时间
        self.dead_time_start = 0

        # 连接信号-槽
        self.stop.connect(self.thread_.onStopSignal)
        thread.progressChanged.connect(self.onSetProgress)
        thread.messageChanged.connect(self.onSetMessage)
        self.stopButton.clicked.connect(self.onCancelButtonClicked)

        self.thread_.hasFinished.connect(self.onFinished)
        self.thread_.canceled.connect(self.onStopped)
        self.thread_.finished.connect(self.onThreadExited) # QThread.finished
        self.thread_.started.connect(self.onThreadStart)
        self.thread_.setIndeterminate.connect(self.onSetIndeterminate)
        self.thread_.deadTime.connect(self.onSetDeadTime)

    def connectMonitorThread(self, thread: "ProcessThread"):
        """
        连接一个辅助监视线程到本组件。监视线程的状态（开始，结束，撤销，成功完成）不会影响本组件的状态。
        监视线程只能设置本组件的信息，进度，采用非确定性进度条还是确定性的、以及与组件关联的线程超时未响应后被杀死的时间，不能改变其他内容
        应当在监视线程和被监视线程之间使用合适的信号-槽进行通信。
        监视线程需要自行启动。
        """
        thread.progressChanged.connect(self.onSetProgress)
        thread.messageChanged.connect(self.onSetMessage)
        thread.setIndeterminate.connect(self.onSetIndeterminate)
        thread.deadTime.connect(self.onSetDeadTime)

    def disconnectMonitorThread(self, thread: "ProcessThread"):
        """
        断开一个辅助监视线程到本组件的连接
        """
        thread.progressChanged.disconnect(self.onSetProgress)
        thread.messageChanged.disconnect(self.onSetMessage)
        thread.setIndeterminate.disconnect(self.onSetIndeterminate)
        thread.deadTime.disconnect(self.onSetDeadTime)

    @pyqtSlot(bool)
    def onSetIndeterminate(self, value: bool):
        if value:
            self.indeterminateProgressBar.setVisible(True)
            self.progressBar.setVisible(False)
        else:
            self.progressBar.setVisible(True)
            self.indeterminateProgressBar.setVisible(False)

        self.isIndeterminate = value

    @pyqtSlot(float)
    def onSetDeadTime(self, value: float):
        self.thread_dead_time = value

    @pyqtSlot()
    def onFinished(self):
        self._saw_end = True
        if self.hide_on_end:
            self.onHide()
        self.finished.emit()

    @pyqtSlot()
    def onThreadStart(self):
        self.stopped = False
        self._saw_end = False # 每次启动重置
        self.timer.start()

    @pyqtSlot()
    def onThreadExited(self):
        self.timer.stop()
        if self._saw_end:
            return
        logger.warning("%s 线程未发送结束信号即退出", type(self.thread_).__name__)
        self.onStopped()

    @pyqtSlot()
    def onStopped(self):
        self._saw_end = True
        if self.hide_on_end:
            self.onHide()
        if self.stoppable:
            self.stopButton.setEnabled(True)
        self.canceled.emit()

    @pyqtSlot()
    def onHide(self):
        self.setVisible(False)

    @pyqtSlot()
    def onCancelButtonClicked(self):
        self.stopButton.setEnabled(False)
        self.label.setText(self.tr("子线程正在退出"))
        if not self.stopped:
            self.dead_time_start = time.time()
            self.stop.emit()
            # 如果是确定进度条，显示一个进度倒退的动画
            # 我们可以预知显示时间为 当前进度百分比 / 超时时间
            # 具体显示逻辑在 checkProcess 中
            self._speed = self.progressBar.value() / self.thread_dead_time if not self.isIndeterminate else 0
        self.stopped = True

    @pyqtSlot()
    def checkProcess(self):
        if not self.thread_.isRunning():
            # 结束判定统一由 onThreadExited（QThread.finished）负责
            self.timer.stop()
            return
        # 如果已经发送了停止请求，且超过了设定的时间线程仍然没有退出，强制终止线程
        if self.stopped:
            if self.thread_.isRunning() and time.time() - self.dead_time_start > self.thread_dead_time:
                logger.warning(f"{str(self.thread_)} 线程强制退出")
                self.thread_.terminate()
                self.thread_.wait()
                self.onStopped()
                self.timer.stop()
            # 尝试显示倒退动画
            elif not self.isIndeterminate and self.backward_animation:
                new_value = self.progressBar.value() - self._speed / 2
                self.progressBar.setValue(int(new_value))

    @pyqtSlot(int)
    def onSetProgress(self, value: int):
        self.progressBar.setValue(value)

    @pyqtSlot(str)
    def onSetMessage(self, message: str):
        self.label.setText(message)


class ProcessThread(QThread):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.can_run = True
        # 兜底上报的临时标记连接；terminate() 强杀后可能残留，由下一次 run 启动前清理
        self._run_marks = None
        # 本次 run 的 error 标记连接：异常发生时用于把它从 receivers() 计数中剔除
        self._run_error_mark = None

    progressChanged = pyqtSignal(int)
    messageChanged = pyqtSignal(str)
    canceled = pyqtSignal()
    hasFinished = pyqtSignal()
    setIndeterminate = pyqtSignal(bool)
    error = pyqtSignal(str, str)
    deadTime = pyqtSignal(float)

    @pyqtSlot()
    def onStopSignal(self):
        self.can_run = False

    def __init_subclass__(cls, **kwargs):
        """子类 run() 统一兜底：漏网之鱼按下面的规则转成信号，子类不需要改名或包裹。

        ## 上报规则（按优先级排列；error 接收者数量在异常发生时判定）
        - 无外部 error 接收者（含运行中被断开）：
            - 只记录日志并交给 sys.excepthook，不补发任务信号；
            该规则优先于以下全部规则：
            即使异常前已发出结束信号，无接收者导致无人能看到错误，须保留全局异常提示；
        - 异常前已发出 hasFinished/canceled：
            - 只记录日志；
        - 异常前已发出 error：
            - 只补发 canceled，不重复上报 error；
        - 其余情况（异常前未发出任何任务信号）：
            - 依次发出 error 与 canceled。

        ## 注意
        子类 run() 不应调用 super().run() 复用父类实现：父类的包装会先报告失败并正常返回，
        子类无法得知父类逻辑已经失败。
        """
        super().__init_subclass__(**kwargs)
        run = cls.__dict__.get("run")
        if run is None or getattr(run, "_process_thread_guarded", False):
            return

        @functools.wraps(run)
        def guarded_run(self):
            # 线程入口的最后兜底：业务异常无法枚举（标注 noqa 消除 Ruff 提示），在此统一捕获而不是让它冒至全局
            # sys.excepthook；SystemExit / KeyboardInterrupt 继承 BaseException，会照常穿透。
            #
            # 上一次 run 若被 terminate() 强杀，finally 不会执行，标记连接会残留；
            # 复用线程前须先清理。
            self._disconnect_run_marks()

            # 本次 run 已发出的结束/错误信号会被临时标记下来：异常若发生在报告之后，
            # 按下面的规则只记日志或只补结束信号。
            reported: set[str] = set()

            def _mark(name):
                def _slot(*_args):
                    reported.add(name)
                return _slot

            error_mark = _mark("error")
            marks = [
                (self.hasFinished, _mark("hasFinished")),
                (self.canceled, _mark("canceled")),
                (self.error, error_mark),
            ]
            # 先登记再连接：即使连接过程中被强杀，下一次 run 也能按登记的标记清理干净。
            self._run_marks = marks
            # error 标记单独保存：异常发生瞬间用它把内部连接从 receivers() 计数中剔除。
            self._run_error_mark = error_mark
            # DirectConnection 保证标记在 worker 线程内同步可见：兜底在 run() 返回后立即读取。
            for signal, slot in marks:
                signal.connect(slot, Qt.DirectConnection)
            try:
                run(self)
            except Exception as error:  # noqa: BLE001
                self.can_run = False
                if self._external_error_receiver_count() == 0:
                    # 此刻没有外部接收者：错误交给全局异常处理（MainWindow 弹原始 traceback），
                    # 否则此失败只会留在日志里。该判断优先于「已发出结束信号」。
                    # 此路径不补发 error/canceled，界面收尾由 ProcessWidget 基于 QThread.finished 的兜底完成。
                    # 直接调用 sys.excepthook 不使用 raise：PyQt5 对逃逸的子线程异常在
                    # sys.excepthook 为默认实现时会 qFatal 终止整个进程；直接调用同样保留
                    # 全局弹窗，且不依赖 hook 是否安装。
                    logger.error(
                        "%s 后台任务失败：%s（error 无外部接收者，转交全局异常处理）",
                        type(self).__name__, type(error).__name__, exc_info=True)
                    sys.excepthook(type(error), error, error.__traceback__)
                    return
                if reported & {"hasFinished", "canceled"}:
                    logger.error(
                        "%s 后台任务在已发出结束/错误信号（%s）后仍抛出异常：%s",
                        type(self).__name__, "、".join(sorted(reported)),
                        type(error).__name__, exc_info=True)
                    return
                if "error" in reported:
                    # 业务代码已经上报过错误：只补发结束信号。
                    self._finish_run_after_reported_error(error)
                    return
                self._report_run_error(error)
            finally:
                # 强杀（terminate）不会执行到这里；
                # 残留标记由下一次 run 启动前的清理兜底。
                self._disconnect_run_marks()

        guarded_run._process_thread_guarded = True
        cls.run = guarded_run

    def _disconnect_run_marks(self) -> None:
        """断开兜底标记连接：支持重复调用、未初始化与对象已销毁等情形。

        强杀（terminate）不会执行 guarded_run 的 finally，残留连接由下一次 run 启动前调用本方法清理。

        disconnect 可能因连接被外部移除（TypeError）或 C++ 对象已销毁（RuntimeError）而失败：兜底本身不能抛出新的异常。
        """
        marks = getattr(self, "_run_marks", None) or ()
        self._run_marks = None
        self._run_error_mark = None
        for signal, slot in marks:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _external_error_receiver_count(self) -> int:
        """异常发生时，统计 error 的外部接收者数量（剔除本次 run 的内部标记连接）。

        内部标记连接会让 receivers() 至少为 1，直接计数会把标记当成业务接收者；
        这里先断开标记再计数。

        标记也可能已被外部整体 disconnect() 一并移除（如账号切换），
        此时 disconnect 抛 TypeError，剩余数量即外部接收者数量。
        该计数不能缓存：界面会在运行期新增或断开连接。

        receivers() 返回 0 或负值（Qt 对未知情形的返回值）时按 0 处理，显式提示异常。
        """
        total = self.receivers(self.error)
        if total <= 0:
            return 0
        if self._run_error_mark is not None:
            mark = self._run_error_mark
            self._run_error_mark = None
            try:
                self.error.disconnect(mark)
            except TypeError:
                pass  # 标记已随外部整体断开被移除：total 即外部数量
            else:
                total -= 1  # 剔除仍在连接的内部标记
        return max(0, total)

    def _report_run_error(self, error: Exception) -> None:
        kind = type(error).__name__
        detail = str(error).strip() or kind
        logger.error("%s 后台任务失败：%s", type(self).__name__, kind, exc_info=True)  # 携带线程名称
        self.error.emit(self.tr("操作失败"), detail)
        self.canceled.emit()

    def _finish_run_after_reported_error(self, error: Exception) -> None:
        """业务代码已上报 error 后异常退出：只补发 canceled，避免重复上报同一次失败。"""
        logger.error(
            "%s 后台任务在已上报错误后仍抛出异常：%s（补发 canceled）",
            type(self).__name__, type(error).__name__, exc_info=True)
        self.canceled.emit()


class ProcessDialog(MessageBoxBase):
    def __init__(self, thread: "ProcessThread", parent=None, stoppable=False):
        super().__init__(parent)
        widget = ProcessWidget(thread, self, stoppable, False)
        self.viewLayout.addWidget(widget)
        self.buttonGroup.setVisible(False)

        thread.hasFinished.connect(self.onThreadFinished)
        thread.canceled.connect(self.onThreadCanceled)
        thread.finished.connect(self.onThreadFinished)
        # 线程被强行终止时发送信号
        widget.canceled.connect(self.onThreadCanceled)

    @pyqtSlot()
    def onThreadCanceled(self):
        self.reject()
        self.rejected.emit()

    @pyqtSlot()
    def onThreadFinished(self):
        self.accept()
        self.accepted.emit()


if __name__ == '__main__':
    class TimerThread(ProcessThread):
        def run(self):
            count = 0
            while count <= 10:
                self.messageChanged.emit(f"{count * 10}%")
                self.progressChanged.emit(count * 10)
                if count == 5 or count == 6:
                    self.setIndeterminate.emit(True)
                else:
                    self.setIndeterminate.emit(False)
                if not self.can_run:
                    self.canceled.emit()
                    return
                time.sleep(1)
                count += 1
            self.hasFinished.emit()

    from PyQt5.QtWidgets import QVBoxLayout, QApplication
    app = QApplication(sys.argv)
    thread = TimerThread()
    wrapper = QFrame()
    layout = QVBoxLayout(wrapper)
    process_widget = ProcessWidget(thread, stoppable=True, parent=wrapper)
    layout.addWidget(process_widget)
    wrapper.show()
    thread.start()
    app.exec()
