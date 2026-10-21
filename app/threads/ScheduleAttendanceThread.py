import datetime
import json
import time

import requests
from PyQt5.QtCore import pyqtSignal

from attendance import Attendance
from auth import ServerError
from .ProcessWidget import ProcessThread
from ..sessions.attendance_session import AttendanceSession
from ..utils import accounts, logger, cfg, request_mfa
from ..utils.mfa import MFACancelledError, MFAUnavailableError
from ..utils.qrcode_login import QRCodeLoginCancelledError, QRCodeLoginUnavailableError


class ScheduleAttendanceThread(ProcessThread):
    # 返回的结果：第一个结果为考勤信息，第二个结果为考勤流水
    result = pyqtSignal(list, list)
    # 获取考勤流水完成（与监视线程通信）
    water_page_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.util = None
        self.start_date = None
        self.end_date = None
        # 考勤流水（打卡信息）
        self.water_page = []
        # 考勤信息
        self.records = []
        # 本次任务使用的账户快照，由 run() 在任务开始时设置
        self.account = None

    @property
    def session(self) -> AttendanceSession:
        return self.account.session_manager.get_session("attendance")

    def login(self) -> None:
        """按照统一访问策略登录考勤系统。"""
        self.setIndeterminate.emit(True)
        self.messageChanged.emit(self.tr("正在登录考勤系统..."))
        self.session.ensure_login(
            self.account.username,
            self.account.password,
            is_postgraduate=self.account.type == self.account.POSTGRADUATE,
            account=self.account,
            mfa_provider=self.account.session_manager.mfa_provider,
        )
        self.messageChanged.emit(self.tr("登录考勤系统成功。"))

    def run(self):
        # 强制重置可运行状态
        self.can_run = True
        # 清除结果
        self.water_page = []
        self.records = []
        # 任务开始时快照账户，后续 session/登录/错误处理统一使用它，避免中途被移除或切换后混用
        self.account = accounts.current
        # 判断当前是否存在账户
        if self.account is None:
            self.error.emit(self.tr("未登录"), self.tr("请先添加一个账户"))
            self.canceled.emit()
            return
        if self.start_date is None or self.end_date is None:
            self.error.emit(self.tr("未选择日期"), self.tr("请先选择日期"))
            self.canceled.emit()
            return

        try:
            # 如果当前账户已经登录并且登录态仍有效，重建代理对象，防止 util 和 session 不对应。
            if self.session.has_login and self.session.validate_login():
                # 如果当前 session 已经登录，必须沿用当前登录方式。
                self.util = Attendance(self.session, is_postgraduate=self.account.type == self.account.POSTGRADUATE)
            else:
                self.login()
                self.util = Attendance(self.session, is_postgraduate=self.account.type == self.account.POSTGRADUATE)
                if not self.can_run:
                    self.canceled.emit()
                    return

            # 查询考勤流水
            self.setIndeterminate.emit(False)
            self.progressChanged.emit(33)
            self.messageChanged.emit(self.tr("正在查询考勤流水..."))
            today = datetime.date.today()
            while True:
                try:
                    if self.start_date <= today <= self.end_date:
                        water_page = self.util.getFlowRecordWithPage(1, 50)["data"]
                    else:
                        water_page = self.util.getFlowRecordByTime(self.start_date.strftime("%Y-%m-%d"), self.end_date.strftime("%Y-%m-%d"))
                except (ServerError, requests.Timeout, json.JSONDecodeError):
                    if cfg.autoRetryAttendance.value:
                        self.error.emit("", self.tr("查询考勤流水失败，正在重试..."))
                        logger.exception("查询考勤流水失败，正在重试...")
                        time.sleep(2)
                        continue
                    else:
                        raise
                else:
                    break

            self.water_page = water_page
            self.water_page_finished.emit()

            if not self.can_run:
                self.result.emit([], water_page)
                self.hasFinished.emit()
                return

            self.progressChanged.emit(66)
            self.messageChanged.emit(self.tr("正在查询考勤信息..."))
            self.records = self.util.attendanceDetailByTime(
                self.start_date.strftime("%Y-%m-%d"),
                self.end_date.strftime("%Y-%m-%d"), 1, 50)
            self.progressChanged.emit(100)

        except Exception as e:
            self.report_error(e)
        else:
            self.result.emit(self.records, water_page)
            self.hasFinished.emit()
