# 研究生查询课表的线程
from PyQt5.QtCore import pyqtSignal

from gmis.schedule import GraduateSchedule
from ..sessions.gmis_session import GMISSession
from ..threads.ProcessWidget import ProcessThread
from ..utils import accounts


class GraduateScheduleThread(ProcessThread):
    """
    获取课表相关信息的线程
    """
    # 获得课表成功后发送的数据
    # {"lessons": 课表信息, "term_number": 学期编号, "start_date": 学期开始日期}
    schedule = pyqtSignal(dict)

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
    def session(self) -> GMISSession:
        """
        获取任务账户用于访问 ehall 的 session
        """
        return self.account.session_manager.get_session("gmis")

    def login(self):
        """
        使当前账户的 session 登录研究生管理信息系统
        """
        self.setIndeterminate.emit(True)
        self.messageChanged.emit(self.tr("正在登录研究生管理信息系统..."))
        self.session.ensure_login(
            self.account.username,
            self.account.password,
            account=self.account,
            mfa_provider=self.account.session_manager.mfa_provider,
        )
        if not self.can_run:
            return False

        # 进入课表页面
        self.util = GraduateSchedule(self.session)
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
        if self.account.type != self.account.POSTGRADUATE:
            self.error.emit(self.tr("账户类型错误"), self.tr("当前账户不是研究生账户，请切换到研究生账户后重试。"))
            self.canceled.emit()
            return

        try:
            result = self.login()
            if not result:
                self.canceled.emit()
                return

            self.progressChanged.emit(66)
            self.messageChanged.emit("正在获取课表信息...")
            result = self.util.getSchedule(timestamp=self.term_number)

            self.progressChanged.emit(88)
            self.messageChanged.emit("正在获取学期开始时间...")

            date_map = self.util.getStartOfTermMap()
            term_string = self.util.getCurrentTerm()
            start_date = date_map.get(self.term_number or term_string, None)

            self.progressChanged.emit(100)

        except Exception as e:
            self.report_error(e)
        else:
            self.schedule.emit({
                "lessons": result,
                "term_number": self.term_number if self.term_number is not None else term_string,
                "start_date": start_date
            })
            self.hasFinished.emit()
