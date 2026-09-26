import os
import sys
import unittest
from pathlib import Path

TEST_DOMAIN = "qt-ui"
TEST_REGRESSION = True

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_STATE_HOME", "/tmp/xjtu-test-state")
os.environ.setdefault("XDG_CONFIG_HOME", "/tmp/xjtu-test-config")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from app.threads.ProcessWidget import ProcessThread

REPO_ROOT = Path(__file__).resolve().parents[2]

if QApplication.instance() is None:
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

APP = QApplication.instance() or QApplication([])


class RunGuardThread(ProcessThread):
    """run() 抛出漏网之鱼，且 error 信号不连接任何接收者。"""

    def run(self):
        raise ValueError("boom")


class ExcepthookDispatchTest(unittest.TestCase):
    """无接收者兜底 re-raise 后，异常经真实 Qt 分发进入 sys.excepthook 且进程存活。"""

    def test_unconnected_error_reraised_to_sys_excepthook_via_start(self):
        seen = []

        def hook(ty, value, tb):
            seen.append((ty, value))
            return sys.__excepthook__(ty, value, tb)

        old_hook = sys.excepthook
        sys.excepthook = hook
        try:
            thread = RunGuardThread()  # 不连接 error：异常应交给全局处理
            thread.start()
            self.assertTrue(thread.wait(5000))
        finally:
            sys.excepthook = old_hook

        # 兜底已执行（can_run 被置 False），re-raise 的异常被交给自定义 hook
        self.assertFalse(thread.can_run)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], ValueError)


class StartupHookOrderTest(unittest.TestCase):
    """确保先安装全局 excepthook、再执行启动期任务的次序不被改动。

    兜底的「无接收者回退全局提示」路径依赖 MainWindow 先行安装 sys.excepthook：
    未安装时 PyQt5 对逃逸的子线程异常会直接 qFatal 终止进程。
    若未来把启动期任务挪到 hook 安装之前，本测试立即失败。
    """

    def test_migrate_all_runs_after_hook_installed(self):
        source = (REPO_ROOT / "app" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("sys.excepthook = self.catchExceptions", source)
        self.assertIn("if migrate_all():", source)
        self.assertLess(
            source.index("sys.excepthook = self.catchExceptions"),
            source.index("if migrate_all():"),
        )


if __name__ == "__main__":
    unittest.main()
