import sys
import json
import asyncio
import threading
import base64
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton,
    QTextEdit, QLineEdit, QFileDialog, QVBoxLayout, QHBoxLayout,
    QSplitter, QMessageBox
)
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer


# ---------- Desktop <-> networking thread ----------

class Events(QObject):
    log = Signal(str)
    status = Signal(str)
    local_sdp = Signal(str)
    file_received = Signal(str)


events = Events()
loop = asyncio.new_event_loop()
pc = None
channel = None
pending_files = {}


def network_thread():
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=network_thread, daemon=True).start()


def submit(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop)


def make_peer():
    global pc
    if pc is not None:
        return pc

    config = RTCConfiguration(iceServers=[
        RTCIceServer(urls=["stun:stun.l.google.com:19302"])
    ])
    pc = RTCPeerConnection(configuration=config)

    @pc.on("connectionstatechange")
    async def connection_state_changed():
        events.status.emit(f"Connection: {pc.connectionState}")

    @pc.on("datachannel")
    def incoming_channel(ch):
        attach_channel(ch)

    return pc


def attach_channel(ch):
    global channel
    channel = ch

    @ch.on("open")
    def opened():
        events.status.emit("Connected — peer-to-peer channel ready")

    @ch.on("close")
    def closed():
        events.status.emit("Data channel closed")

    @ch.on("message")
    def message_received(message):
        if isinstance(message, bytes):
            return
        try:
            data = json.loads(message)
            kind = data.get("type")

            if kind == "text":
                events.log.emit(f'{data.get("name", "Friend")}: {data["text"]}')

            elif kind == "file_start":
                filename = Path(data["name"]).name
                downloads = Path.home() / "Downloads" / "PeerMessenger"
                downloads.mkdir(parents=True, exist_ok=True)
                target = downloads / f'{uuid.uuid4().hex[:8]}_{filename}'
                pending_files[data["id"]] = {
                    "path": target,
                    "file": target.open("wb"),
                }
                events.log.emit(f"Receiving file: {filename}")

            elif kind == "file_chunk":
                item = pending_files.get(data["id"])
                if item:
                    item["file"].write(base64.b64decode(data["data"]))

            elif kind == "file_end":
                item = pending_files.pop(data["id"], None)
                if item:
                    item["file"].close()
                    events.file_received.emit(str(item["path"]))

        except (ValueError, KeyError, OSError) as exc:
            events.log.emit(f"Could not process incoming message: {exc}")


async def create_offer():
    peer = make_peer()
    ch = peer.createDataChannel("chat", ordered=True)
    attach_channel(ch)

    await peer.setLocalDescription(await peer.createOffer())
    events.local_sdp.emit(peer.localDescription.sdp)
    events.status.emit("Offer created. Copy it to the other PC.")


async def create_answer(offer_sdp):
    peer = make_peer()
    await peer.setRemoteDescription(
        RTCSessionDescription(sdp=offer_sdp, type="offer")
    )
    await peer.setLocalDescription(await peer.createAnswer())
    events.local_sdp.emit(peer.localDescription.sdp)
    events.status.emit("Answer created. Send it back to the offerer.")


async def apply_answer(answer_sdp):
    if pc is None:
        raise RuntimeError("Create an offer first.")
    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=answer_sdp, type="answer")
    )
    events.status.emit("Answer applied. Connecting...")


async def send_json(data):
    if channel is None or channel.readyState != "open":
        raise RuntimeError("The peer-to-peer connection is not open.")
    channel.send(json.dumps(data))


async def send_file(path):
    if channel is None or channel.readyState != "open":
        raise RuntimeError("Connect to the other PC first.")

    file_id = uuid.uuid4().hex
    filename = Path(path).name
    size = Path(path).stat().st_size

    await send_json({
        "type": "file_start", "id": file_id,
        "name": filename, "size": size
    })

    with open(path, "rb") as file:
        while True:
            chunk = file.read(24 * 1024)
            if not chunk:
                break
            await send_json({
                "type": "file_chunk",
                "id": file_id,
                "data": base64.b64encode(chunk).decode("ascii")
            })
            # Avoid flooding the data channel's send buffer.
            await asyncio.sleep(0.01)

    await send_json({"type": "file_end", "id": file_id})
    events.log.emit(f"You sent file: {filename}")


# ---------- Desktop interface ----------

class Messenger(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PeerMessenger")
        self.resize(1000, 720)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)

        split = QSplitter()
        layout.addWidget(split)

        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 22, 18, 18)

        logo = QLabel("◈  PeerMessenger")
        logo.setObjectName("logo")
        side.addWidget(logo)

        side.addSpacing(20)
        side.addWidget(QLabel("YOUR CONNECTION"))
        side.addWidget(QLabel("Direct peer-to-peer"))
        side.addWidget(QLabel("No chat account required"))
        side.addStretch()

        self.status = QLabel("Not connected")
        self.status.setWordWrap(True)
        side.addWidget(self.status)
        split.addWidget(sidebar)

        main = QWidget()
        content = QVBoxLayout(main)
        content.setContentsMargins(22, 18, 22, 18)

        header = QHBoxLayout()
        title = QLabel("Private chat")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(QLabel("●  P2P"))
        content.addLayout(header)

        content.addWidget(QLabel(
            "1. Create an offer on one PC. On the other, paste it and create an answer."
        ))

        self.remote = QTextEdit()
        self.remote.setPlaceholderText("Paste the other PC's SDP offer or answer here...")
        self.remote.setMaximumHeight(100)
        content.addWidget(self.remote)

        actions = QHBoxLayout()
        for label, callback in [
            ("Create offer", self.offer),
            ("Create answer", self.answer),
            ("Apply answer", self.apply),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
        content.addLayout(actions)

        self.local = QTextEdit()
        self.local.setReadOnly(True)
        self.local.setPlaceholderText("Your connection description appears here...")
        self.local.setMaximumHeight(100)
        content.addWidget(self.local)

        content.addWidget(QLabel("MESSAGES"))
        self.chat = QTextEdit()
        self.chat.setReadOnly(True)
        content.addWidget(self.chat, 1)

        composer = QHBoxLayout()
        self.message = QLineEdit()
        self.message.setPlaceholderText("Write a message...")
        self.message.returnPressed.connect(self.send_text)
        composer.addWidget(self.message, 1)

        send = QPushButton("Send")
        send.clicked.connect(self.send_text)
        composer.addWidget(send)

        attach = QPushButton("Send file")
        attach.clicked.connect(self.choose_file)
        composer.addWidget(attach)
        content.addLayout(composer)

        split.addWidget(main)
        split.setSizes([230, 770])

        self.setStyleSheet("""
            QWidget {
                background: #11151c;
                color: #e6eaf0;
                font-size: 13px;
            }
            QWidget#unused { background: #171d27; }
            QLabel#logo {
                font-size: 19px;
                font-weight: 700;
                color: #83b6ff;
            }
            QLabel#title { font-size: 23px; font-weight: 700; }
            QTextEdit, QLineEdit {
                background: #1b2330;
                border: 1px solid #303b4d;
                border-radius: 8px;
                padding: 9px;
                selection-background-color: #366ed8;
            }
            QPushButton {
                background: #2d65d5;
                border: none;
                border-radius: 7px;
                padding: 10px 14px;
                font-weight: 600;
            }
            QPushButton:hover { background: #4279e6; }
            QSplitter::handle { background: #303744; }
        """)

        events.log.connect(self.add_log)
        events.status.connect(self.status.setText)
        events.local_sdp.connect(self.local.setPlainText)
        events.file_received.connect(self.received_file)

    def add_log(self, text):
        self.chat.append(text)

    def offer(self):
        submit(create_offer())

    def answer(self):
        sdp = self.remote.toPlainText().strip()
        if sdp:
            submit(create_answer(sdp))
        else:
            QMessageBox.information(self, "Offer needed", "Paste the offer first.")

    def apply(self):
        sdp = self.remote.toPlainText().strip()
        if sdp:
            future = submit(apply_answer(sdp))
            future.add_done_callback(
                lambda f: events.log.emit(f"Connection setup error: {f.exception()}")
                if f.exception() else None
            )
        else:
            QMessageBox.information(self, "Answer needed", "Paste the answer first.")

    def send_text(self):
        text = self.message.text().strip()
        if not text:
            return

        future = submit(send_json({"type": "text", "name": "You", "text": text}))
        def done(f):
            try:
                f.result()
                events.log.emit(f"You: {text}")
            except Exception as exc:
                events.log.emit(f"Send failed: {exc}")
        future.add_done_callback(done)
        self.message.clear()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file")
        if path:
            future = submit(send_file(path))
            future.add_done_callback(
                lambda f: events.log.emit(f"File transfer error: {f.exception()}")
                if f.exception() else None
            )

    def received_file(self, path):
        self.chat.append(f"File saved: {path}")

    def closeEvent(self, event):
        if pc is not None:
            submit(pc.close())
        loop.call_soon_threadsafe(loop.stop)
        event.accept()


app = QApplication(sys.argv)
app.setStyle("Fusion")
window = Messenger()
window.show()
sys.exit(app.exec())