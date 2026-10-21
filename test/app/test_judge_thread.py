import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

TEST_DOMAIN = "qt-ui"
TEST_REGRESSION = True

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from app.threads.JudgeThread import JudgeChoice, JudgeThread

if QApplication.instance() is None:
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

APP = QApplication.instance() or QApplication([])


class JudgeThreadFailureLoggingTest(unittest.TestCase):
    """单门课程异常时的日志分支依赖模块内的 logger 导入。"""

    def test_questionnaire_exception_logs_and_does_not_abort(self):
        thread = JudgeThread(Mock(), JudgeChoice.JUDGE_ALL)
        thread.judge_ = Mock()
        thread.judge_.unfinishedQuestionnaires.return_value = [
            SimpleNamespace(KCM="课程1", WJMC="理论课"),
        ]
        thread.judge_.questionnaireData.side_effect = RuntimeError("boom")
        errors = []
        thread.error.connect(lambda title, _detail: errors.append(title))

        with patch("app.threads.JudgeThread.logger") as logger:
            result = thread.judge_all()

        logger.error.assert_called_once()
        self.assertFalse(result)
        self.assertEqual(errors, [""])


if __name__ == "__main__":
    unittest.main()
