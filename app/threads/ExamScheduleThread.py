from PyQt5.QtCore import pyqtSignal

from jwxt.schedule import Schedule
from .ProcessWidget import ProcessThread
from ..sessions.jwxt_session import JWXTSession
from ..utils.account import accounts


class ExamScheduleThread(ProcessThread):
    """
    获取考试信息的线程。获得课表时会自动获得一次考试信息，所以使用此线程并不是完全必须的。
    """
    # 获得考试信息成功后发送的数据
    # {"exams": 课表信息, "term_number": 学期编号}
    exam = pyqtSignal(dict)

    def __init__(self, term_number=None, parent=None):
        """
        创建一个获取课表的线程
        :param term_number: 需要获取的课表的学期编号
        :param parent: 父对象
        """
        super().__init__(parent)
        self.util = None
        self.term_number = term_number
        # 本次任务使用的账户快照，由 run() 在任务开始时设置
        self.account = None

    @property
    def session(self) -> JWXTSession:
        """
        获取任务账户用于访问教务系统的 session
        """
        return self.account.session_manager.get_session("jwxt")

    def login(self):
        """
        使当前账户的 session 登录教务系统
        """
        self.setIndeterminate.emit(True)
        self.messageChanged.emit(self.tr("正在登录教务系统..."))
        self.session.ensure_login(
            self.account.username,
            self.account.password,
            account=self.account,
            mfa_provider=self.account.session_manager.mfa_provider,
        )

        if not self.can_run:
            return False

        # 进入课表页面
        self.util = Schedule(self.session)
        self.setIndeterminate.emit(False)

        return True

    def run(self):
        # 强制重置可运行状态
        self.can_run = True
        # 任务开始时快照账户，后续 session/登录/错误处理统一使用它，避免中途被移除或切换后混用
        self.account = accounts.current
        # 判断当前是否存在账户
        if self.account is None:
            self.error.emit(self.tr("未登录"), self.tr("请先添加一个账户"))
            self.canceled.emit()
            return

        try:
            result = self.login()
            if not result:
                self.canceled.emit()
                return

            self.progressChanged.emit(66)
            self.messageChanged.emit("正在获取考试时间...")
            exam = self.util.getExamSchedule(timestamp=self.term_number)

            self.progressChanged.emit(100)

        except Exception as e:
            self.report_error(e)
        else:
            self.exam.emit({
                "exams": exam,
                "term_number": self.term_number if self.term_number is not None else self.util.termString
            })
            self.hasFinished.emit()
