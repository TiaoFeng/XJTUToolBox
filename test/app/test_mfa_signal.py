import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

TEST_DOMAIN = "qt-ui"
TEST_REGRESSION = True

from app.threads.ExamScheduleThread import ExamScheduleThread
from app.threads.JudgeThread import JudgeChoice, JudgeThread
from app.utils.account import request_mfa
from auth import ServerError


def _switch_and_raise(error, holder, new_current):
    """模拟登录请求进行中账户被移除/切换。"""

    def side_effect(*args, **kwargs):
        holder.current = new_current
        raise error

    return side_effect


class RequestMFATest(unittest.TestCase):
    def test_none_account_is_ignored(self):
        request_mfa(None)  # 不应抛异常

    def test_account_is_notified(self):
        account = SimpleNamespace(MFASignal=Mock())
        request_mfa(account)
        account.MFASignal.emit.assert_called_once_with(True)


class MFA102TargetTest(unittest.TestCase):
    def test_account_removed_mid_login_still_reports_failure(self):
        """旧实现：accounts.current 为 None，AttributeError 逃出 run()，canceled 丢失。"""
        thread = ExamScheduleThread()
        errors, canceled = [], []
        thread.error.connect(lambda title, detail: errors.append((title, detail)))
        thread.canceled.connect(lambda: canceled.append(True))

        account = SimpleNamespace(
            username="u", password="p", MFASignal=Mock(), session_manager=Mock()
        )
        holder = SimpleNamespace(current=account)
        account.session_manager.get_session.return_value.ensure_login.side_effect = (
            _switch_and_raise(ServerError(102, "mfa"), holder, None)
        )

        with patch("app.threads.ExamScheduleThread.accounts", holder):
            thread.run()

        self.assertEqual(1, len(errors))
        self.assertEqual("登录问题", errors[0][0])
        self.assertEqual([True], canceled)
        account.MFASignal.emit.assert_called_once_with(True)

    def test_switch_mid_login_notifies_owning_account(self):
        """旧实现：MFA 信号会发给中途切换成的新账户。"""
        first = SimpleNamespace(
            username="a", password="ap", MFASignal=Mock(), session_manager=Mock()
        )
        second = SimpleNamespace(
            username="b", password="bp", MFASignal=Mock(), session_manager=Mock()
        )
        holder = SimpleNamespace(current=first)
        first.session_manager.get_session.return_value.ensure_login.side_effect = (
            _switch_and_raise(ServerError(102, "mfa"), holder, second)
        )

        thread = JudgeThread(first, JudgeChoice.GET_COURSES)
        canceled = []
        thread.canceled.connect(lambda: canceled.append(True))

        # 当前实现已不再从模块读取 accounts；create=True 保证同一用例在新旧实现上都能运行
        with patch("app.threads.JudgeThread.accounts", holder, create=True):
            thread.run()

        first.MFASignal.emit.assert_called_once_with(True)
        second.MFASignal.emit.assert_not_called()
        self.assertEqual([True], canceled)


if __name__ == "__main__":
    unittest.main()
