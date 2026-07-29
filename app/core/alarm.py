"""警報：循環音效 + 置頂警告視窗。

原本寫在「監控技能經驗球」分頁裡，因為「收益監控」也要用，抽出來共用。
兩邊都是同一顆警報聲、同一種視窗行為，沒必要維護兩份。

音效優先播 music/Alarm_music.mp3（循環到 stop()）；播不出來就退回 winsound 嗶聲。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

from app.paths import resource

ALARM_MP3 = "music/Alarm_music.mp3"


class BeepThread(QThread):
    """後備方案：mp3 播不出來時，用循環嗶聲，直到 stop()。"""

    def __init__(self) -> None:
        super().__init__()
        self._on = True

    def run(self) -> None:
        try:
            import winsound
        except Exception:
            return
        while self._on:
            try:
                winsound.Beep(1000, 500)
            except Exception:
                self.msleep(500)
            self.msleep(250)

    def stop(self) -> None:
        self._on = False


class Alarm:
    """警報聲：優先『循環播放 music/Alarm_music.mp3』；播不出來則退回嗶聲。

    QMediaPlayer 必須在有事件迴圈的執行緒建立/使用，因此本類別由 UI 主執行緒持有
    與操作（警報是在 UI 執行緒觸發的）。
    """

    def __init__(self, parent=None) -> None:
        self._parent = parent
        self._player = None
        self._audio = None
        self._beep: BeepThread | None = None

    def start(self) -> None:
        if self._player is not None or (self._beep and self._beep.isRunning()):
            return  # 已經在響
        if not self._start_mp3():
            self._beep = BeepThread()
            self._beep.start()

    def _start_mp3(self) -> bool:
        path = resource(ALARM_MP3)
        if not path.exists():
            return False
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            self._audio = QAudioOutput()
            self._player = QMediaPlayer(self._parent)
            self._player.setAudioOutput(self._audio)
            self._player.setLoops(QMediaPlayer.Loops.Infinite)  # 循環播放到 stop()
            self._player.setSource(QUrl.fromLocalFile(str(path)))
            self._player.play()
            return True
        except Exception:
            self._player = None
            self._audio = None
            return False

    def stop(self) -> None:
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
            self._player = None
            self._audio = None
        if self._beep is not None:
            self._beep.stop()
            self._beep.wait(2000)
            self._beep = None


class AlarmDialog(QDialog):
    """置頂的警告視窗，內容由呼叫端決定，含「停止警報」按鈕。"""

    def __init__(self, parent, on_stop, title: str = "⚠ 警報") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(420, 220)
        lay = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setStyleSheet("font-size: 14px;")
        lay.addWidget(self.label)
        lay.addStretch(1)
        btn = QPushButton("停止警報")
        btn.setStyleSheet("font-size: 14px; padding: 8px;")
        btn.clicked.connect(on_stop)
        lay.addWidget(btn)

    def set_text(self, text: str) -> None:
        self.label.setText(text)

    def popup(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
