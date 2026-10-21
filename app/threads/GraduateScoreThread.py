import requests
from PyQt5.QtCore import pyqtSignal

from auth.new_login import NewLogin
from gmis.score import GraduateScore
from ..sessions.gmis_session import GMISSession
from ..threads.ProcessWidget import ProcessThread
from ..utils import accounts, logger, cfg, request_mfa
from ..utils.mfa import MFACancelledError, MFAUnavailableError
from ..utils.qrcode_login import QRCodeLoginCancelledError, QRCodeLoginUnavailableError
from auth import ServerError, GMIS_LOGIN_URL


class GraduateScoreThread(ProcessThread):
    """
    获取研究生成绩相关的线程
    """
    # 成绩获取成功后发送的数据
    # 成绩列表、是否为研究生成绩（此线程固定发送 True）
    scores = pyqtSignal(list, bool)

    def __init__(self, parent=None):
        """
        创建一个获取研究生成绩的线程
        :param parent: 父对象
        """
        super().__init__(parent)
        self.util = None
        self.allow_qrcode_login = True
        # 本次任务使用的账户快照，由 run() 在任务开始时设置
        self.account = None

    @property
    def session(self) -> GMISSession:
        """
        获取任务账户用于访问 gmis 的 session
        """
        return self.account.session_manager.get_session("gmis")

    def login(self):
        """
        使当前账户的 session 登录 gmis
        """
        self.setIndeterminate.emit(True)
        self.messageChanged.emit(self.tr("正在登录研究生信息管理系统..."))
        self.session.ensure_login(
            self.account.username,
            self.account.password,
            account=self.account,
            mfa_provider=self.account.session_manager.mfa_provider,
            allow_qrcode_login=self.allow_qrcode_login,
        )
        if not self.can_run:
            return False

        self.util = GraduateScore(self.session)
        self.setIndeterminate.emit(False)

        return True

    def run(self):
        """
        获取成绩的主要逻辑
        """
        self.can_run = True
        # 任务开始时快照账户，后续 session/登录/错误处理统一使用它，避免中途被移除或切换后混用
        self.account = accounts.current
        # 判断当前是否存在账户
        if self.account is None:
            self.error.emit(self.tr("未登录"), self.tr("请先添加一个账户"))
            self.canceled.emit()
            return

        self.setIndeterminate.emit(False)
        self.progressChanged.emit(0)

        try:
            result = self.login()
            if not result:
                self.canceled.emit()
                return
            self.progressChanged.emit(66)
            self.messageChanged.emit("正在获取成绩...")
            if not self.can_run:
                return
            result = self.util.grade()
            self.progressChanged.emit(100)
        except Exception as e:
            self.report_error(e)
        else:
            self.scores.emit(result, True)
            self.hasFinished.emit()
