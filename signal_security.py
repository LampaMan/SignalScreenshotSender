import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional


@dataclass
class PendingDevice:
    uuid: str
    name: str
    message: str
    source: str = "signal-cli"


@dataclass
class LocalEvent:
    event_id: str
    event_type: str
    payload: Dict[str, Any]
    created_at: str
    signature: str


class SecureSessionManager:
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or os.getcwd()
        self.session_dir = os.path.join(self.base_dir, ".signal_security")
        self.session_token: Optional[str] = None
        self.cert_path: Optional[str] = None
        self.key_path: Optional[str] = None
        self.event_file = os.path.join(self.session_dir, "events.jsonl")

    def initialize(self) -> dict:
        os.makedirs(self.session_dir, exist_ok=True)
        self.session_token = secrets.token_urlsafe(32)
        self.cert_path = os.path.join(self.session_dir, "session.cert")
        self.key_path = os.path.join(self.session_dir, "session.key")

        cert_payload = self._build_certificate_payload()
        key_payload = self._build_key_payload()

        with open(self.cert_path, "w", encoding="utf-8") as fh:
            fh.write(cert_payload)
        with open(self.key_path, "w", encoding="utf-8") as fh:
            fh.write(key_payload)

        os.chmod(self.cert_path, 0o600)
        os.chmod(self.key_path, 0o600)
        self._ensure_event_store()

        return {
            "session_token": self.session_token,
            "cert_path": self.cert_path,
            "key_path": self.key_path,
            "fingerprint": self.get_fingerprint(),
            "event_file": self.event_file,
        }

    def _ensure_event_store(self) -> None:
        os.makedirs(self.session_dir, exist_ok=True)
        if not os.path.exists(self.event_file):
            with open(self.event_file, "w", encoding="utf-8") as fh:
                fh.write("")
            os.chmod(self.event_file, 0o600)

    def get_session_token(self) -> str:
        if not self.session_token:
            self.initialize()
        return self.session_token

    def get_fingerprint(self) -> str:
        token = self.get_session_token()
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def sign_payload(self, payload: str) -> str:
        token = self.get_session_token()
        return hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_signature(self, payload: str, signature: str) -> bool:
        return hmac.compare_digest(self.sign_payload(payload), signature)

    def emit_local_event(self, event_type: str, payload: Dict[str, Any]) -> LocalEvent:
        self._ensure_event_store()
        event_id = hashlib.sha256(f"{event_type}:{json.dumps(payload, sort_keys=True)}".encode("utf-8")).hexdigest()[:16]
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        payload_copy = dict(payload)
        payload_copy["session_fingerprint"] = self.get_fingerprint()
        payload_text = json.dumps(payload_copy, sort_keys=True)
        signature = self.sign_payload(payload_text)
        event = LocalEvent(
            event_id=event_id,
            event_type=event_type,
            payload=payload_copy,
            created_at=created_at,
            signature=signature,
        )
        with open(self.event_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(event), sort_keys=True) + "\n")
        return event

    def load_local_events(self) -> List[LocalEvent]:
        self._ensure_event_store()
        events: List[LocalEvent] = []
        if not os.path.exists(self.event_file):
            return events
        with open(self.event_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    events.append(
                        LocalEvent(
                            event_id=item.get("event_id", ""),
                            event_type=item.get("event_type", ""),
                            payload=item.get("payload", {}),
                            created_at=item.get("created_at", ""),
                            signature=item.get("signature", ""),
                        )
                    )
                except json.JSONDecodeError:
                    continue
        return events

    def publish_pending_device(self, device: PendingDevice) -> LocalEvent:
        payload = {
            "uuid": device.uuid,
            "name": device.name,
            "message": device.message,
            "source": device.source,
        }
        return self.emit_local_event("pending_device", payload)

    def publish_approval_result(self, device: PendingDevice, result: Dict[str, Any]) -> LocalEvent:
        payload = {
            "uuid": device.uuid,
            "name": device.name,
            "result": result,
        }
        return self.emit_local_event("approval_result", payload)

    def _build_certificate_payload(self) -> str:
        token = self.get_session_token()
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return (
            "-----BEGIN SIGNAL SESSION CERTIFICATE-----\n"
            f"fingerprint={fingerprint}\n"
            f"token={token}\n"
            "-----END SIGNAL SESSION CERTIFICATE-----\n"
        )

    def _build_key_payload(self) -> str:
        token = self.get_session_token()
        return (
            "-----BEGIN SIGNAL SESSION KEY-----\n"
            f"token={token}\n"
            "-----END SIGNAL SESSION KEY-----\n"
        )


def parse_signal_cli_output(text: str) -> List[PendingDevice]:
    devices: List[PendingDevice] = []
    if not text:
        return devices

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        lowered = line.lower()
        if "uuid=" not in lowered:
            continue
        if not any(keyword in lowered for keyword in ["mismatch", "approval", "pending", "new device", "device"]):
            continue

        match = re.search(r"uuid=([0-9a-fA-F-]{8,})", line)
        if not match:
            continue

        device_uuid = match.group(1)
        name_match = re.search(r"name=([^\s,]+)", line)
        name = name_match.group(1) if name_match else "unknown"
        devices.append(PendingDevice(uuid=device_uuid, name=name, message=line))

    return devices


class SignalDaemonMonitor:
    def __init__(self, settings: dict, security_manager: SecureSessionManager, callback: Optional[Callable[[PendingDevice], None]] = None):
        self.settings = settings
        self.security_manager = security_manager
        self.callback = callback
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._seen_uuids = set()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def _monitor_loop(self) -> None:
        while self._running:
            try:
                self._start_process()
                if self._process and self._process.stdout:
                    for line in iter(self._process.stdout.readline, ""):
                        if not self._running:
                            break
                        self._handle_line(line)
                if self._process:
                    self._process.wait(timeout=1)
            except Exception as exc:
                print(f"Signal daemon monitor error: {exc}")
            if self._running:
                time.sleep(5)

    def _start_process(self) -> None:
        cli_path = self.settings.get("cli_path")
        signal_recipient = self.settings.get("signal_recipient")
        if not cli_path:
            return

        cmd_candidates = []
        base_cmd = [cli_path]
        if signal_recipient:
            base_cmd.extend(["-a", signal_recipient])
        cmd_candidates.append(base_cmd + ["daemon"])
        cmd_candidates.append(base_cmd + ["receive"])
        cmd_candidates.append([cli_path, "daemon"])

        last_exc = None
        for cmd in cmd_candidates:
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    cwd=os.path.dirname(cli_path) or None,
                )
                return
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        for device in parse_signal_cli_output(line):
            if device.uuid in self._seen_uuids:
                continue
            with self._lock:
                self._seen_uuids.add(device.uuid)
            self.security_manager.publish_pending_device(device)
            if self.callback:
                self.callback(device)


def approve_pending_device(device: PendingDevice, settings: dict, security_manager: SecureSessionManager) -> dict:
    cli_path = settings.get("cli_path")
    signal_recipient = settings.get("signal_recipient")
    approval_command = settings.get("approval_command") or settings.get("approval_command_template")

    if not cli_path:
        raise RuntimeError("Signal CLI path is not configured")

    command: List[str] = []
    if approval_command:
        command = shlex.split(
            approval_command.format(
                cli_path=cli_path,
                signal_recipient=signal_recipient or "",
                uuid=device.uuid,
                name=device.name,
                session_token=security_manager.get_session_token(),
            )
        )
    else:
        command = [cli_path, "-a", signal_recipient or "", "trustNewDevices"]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=os.path.dirname(cli_path) or None,
    )

    security_manager.publish_approval_result(
        device,
        {
            "command": command,
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
            "verified": result.returncode == 0,
        },
    )

    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
        "verified": result.returncode == 0,
    }
