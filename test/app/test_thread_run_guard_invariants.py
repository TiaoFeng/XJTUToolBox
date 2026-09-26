import os
import subprocess
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
    """无接收者兜底直接调用 sys.excepthook，不依赖 PyQt 对逃逸异常的转发。"""

    def test_unconnected_error_reaches_sys_excepthook_via_start(self):
        seen = []

        def hook(ty, value, tb):
            seen.append((ty, value))

        old_hook = sys.excepthook
        sys.excepthook = hook
        try:
            thread = RunGuardThread()  # 不连接 error：异常应交给全局处理
            thread.start()
            self.assertTrue(thread.wait(5000))
        finally:
            sys.excepthook = old_hook

        # 兜底已执行（can_run 被置 False），异常被交给自定义 hook，线程正常结束
        self.assertFalse(thread.can_run)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], ValueError)


DEFAULT_HOOK_SCRIPT = """import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from app.threads.ProcessWidget import ProcessThread
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
app = QApplication([])
class T(ProcessThread):
    def run(self):
        raise ValueError('boom')
t = T()
t.start()
assert t.wait(5000)
print('survived')
"""


class DefaultHookSafetyTest(unittest.TestCase):
    """默认 sys.excepthook 下，无接收者兜底只能打印 traceback，不能终止进程。"""

    def test_no_receiver_with_default_hook_does_not_abort_process(self):
        result = subprocess.run(
            [sys.executable, "-c", DEFAULT_HOOK_SCRIPT],
            check=False,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("survived", result.stdout)


if __name__ == "__main__":
    unittest.main()
