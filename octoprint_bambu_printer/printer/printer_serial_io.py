import logging
import queue
import re
import threading
import time
from typing import Callable

from octoprint.util import to_bytes, to_unicode
from serial import SerialTimeoutException

from .char_counting_queue import CharCountingQueue


class PrinterSerialIO(threading.Thread):
    command_regex = re.compile(r"^([GM])(\d+)")

    def __init__(
        self,
        handle_command_callback: Callable[[str, str, bytes], None],
        settings,
        serial_log_handler=None,
        read_timeout=5.0,
        write_timeout=10.0,
    ) -> None:
        super().__init__(
            name="octoprint.plugins.bambu_printer.wait_thread", daemon=True
        )
        self._handle_command_callback = handle_command_callback
        self._settings = settings
        self._serial_log = logging.getLogger(
            "octoprint.plugins.bambu_printer.BambuPrinter.serial"
        )
        self._serial_log.setLevel(logging.CRITICAL)
        self._serial_log.propagate = False

        if serial_log_handler is not None:
            self._serial_log.addHandler(serial_log_handler)
            self._serial_log.setLevel(logging.INFO)

        self._serial_log.debug("-" * 78)

        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

        self._received_lines = 0
        self._wait_interval = 5.0
        self._running = True

        self._rx_buffer_size = 64
        self._incoming_lock = threading.RLock()

        self.incoming = CharCountingQueue(self._rx_buffer_size, name="RxBuffer")
        self.outgoing = queue.Queue()
        self.buffered = queue.Queue(maxsize=4)
        self.command_queue = queue.Queue()

    @property
    def incoming_lock(self):
        return self._incoming_lock

    def run(self) -> None:
        linenumber = 0
        next_wait_timeout = 0

        def recalculate_next_wait_timeout():
            nonlocal next_wait_timeout
            next_wait_timeout = time.monotonic() + self._wait_interval

        recalculate_next_wait_timeout()

        data = None

        buf = b""
        while self.incoming is not None and self._running:
            try:
                data = self.incoming.get(timeout=0.01)
                data = to_bytes(data, encoding="ascii", errors="replace")
                self.incoming.task_done()
            except queue.Empty:
                continue
            except Exception:
                if self.incoming is None:
                    # just got closed
                    break

            if data is not None:
                buf += data
                nl = buf.find(b"\n") + 1
                if nl > 0:
                    data = buf[:nl]
                    buf = buf[nl:]
                else:
                    continue

            recalculate_next_wait_timeout()

            if data is None:
                continue

            self._received_lines += 1

            # strip checksum
            if b"*" in data:
                checksum = int(data[data.rfind(b"*") + 1 :])
                data = data[: data.rfind(b"*")]
                if not checksum == self._calculate_checksum(data):
                    self._triggerResend(expected=self.current_line + 1)
                    continue

                self.current_line += 1
            elif self._settings.get_boolean(["forceChecksum"]):
                self.send(self._format_error("checksum_missing"))
                continue

            # track N = N + 1
            if data.startswith(b"N") and b"M110" in data:
                linenumber = int(re.search(b"N([0-9]+)", data).group(1))
                self.lastN = linenumber
                self.current_line = linenumber
                self.sendOk()
                continue

            elif data.startswith(b"N"):
                linenumber = int(re.search(b"N([0-9]+)", data).group(1))
                expected = self.lastN + 1
                if linenumber != expected:
                    self._triggerResend(actual=linenumber)
                    continue
                else:
                    self.lastN = linenumber

                data = data.split(None, 1)[1].strip()

            data += b"\n"

            command = to_unicode(data, encoding="ascii", errors="replace").strip()

            # actual command handling
            command_match = self.command_regex.match(command)
            if command_match is not None:
                gcode = command_match.group(0)
                gcode_letter = command_match.group(1)

                self._handle_command_callback(gcode_letter, gcode, data)

            self._serial_log.debug("Closing down read loop")

    def stop(self):
        self._running = False

    def _showPrompt(self, text, choices):
        self._hidePrompt()
        self.send(f"//action:prompt_begin {text}")
        for choice in choices:
            self.send(f"//action:prompt_button {choice}")
        self.send("//action:prompt_show")

    def _hidePrompt(self):
        self.send("//action:prompt_end")

    def write(self, data: bytes) -> int:
        data = to_bytes(data, errors="replace")
        u_data = to_unicode(data, errors="replace")

        with self._incoming_lock:
            if self.is_closed():
                return 0

            try:
                written = self.incoming.put(
                    data, timeout=self._write_timeout, partial=True
                )
                self._serial_log.debug(f"<<< {u_data}")
                return written
            except queue.Full:
                self._serial_log.error(
                    "Incoming queue is full, raising SerialTimeoutException"
                )
                raise SerialTimeoutException()

    def readline(self) -> bytes:
        assert self.outgoing is not None
        timeout = self._read_timeout

        try:
            # fetch a line from the queue, wait no longer than timeout
            line = to_unicode(self.outgoing.get(timeout=timeout), errors="replace")
            self._serial_log.debug(f">>> {line.strip()}")
            self.outgoing.task_done()
            return to_bytes(line)
        except queue.Empty:
            # queue empty? return empty line
            return b""

    def send(self, line: str) -> None:
        if self.outgoing is not None:
            self.outgoing.put(line)

    def sendOk(self):
        if self.outgoing is None:
            return
        self.send("ok")

    def reset(self):
        if self.incoming is not None:
            self._clearQueue(self.incoming)
        if self.outgoing is not None:
            self._clearQueue(self.outgoing)

    def close(self):
        self.stop()
        self.incoming = None
        self.outgoing = None

    def is_closed(self):
        return self.incoming is None or self.outgoing is None

    def _triggerResend(
        self, expected: int = None, actual: int = None, checksum: int = None
    ) -> None:
        with self._incoming_lock:
            if expected is None:
                expected = self.lastN + 1
            else:
                self.lastN = expected - 1

            if actual is None:
                if checksum:
                    self.send(self._format_error("checksum_mismatch"))
                else:
                    self.send(self._format_error("checksum_missing"))
            else:
                self.send(self._format_error("lineno_mismatch", expected, actual))

            def request_resend():
                self.send("Resend:%d" % expected)
                self.sendOk()

            request_resend()

    def _calculate_checksum(self, line: bytes) -> int:
        checksum = 0
        for c in bytearray(line):
            checksum ^= c
        return checksum

    def _format_error(self, error: str, *args, **kwargs) -> str:
        errors = {
            "checksum_mismatch": "Checksum mismatch",
            "checksum_missing": "Missing checksum",
            "lineno_mismatch": "expected line {} got {}",
            "lineno_missing": "No Line Number with checksum, Last Line: {}",
            "maxtemp": "MAXTEMP triggered!",
            "mintemp": "MINTEMP triggered!",
            "command_unknown": "Unknown command {}",
        }
        return f"Error: {errors.get(error).format(*args, **kwargs)}"

    def _clearQueue(self, q: queue.Queue):
        try:
            while q.get(block=False):
                q.task_done()
                continue
        except queue.Empty:
            pass
