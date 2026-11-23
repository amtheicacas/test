"""A tiny SMTP server on 127.0.0.1 for tests: it behaves like a real mail server for the cases the application must cope with, and nothing leaves the machine.

  * an address containing "nobody"    -> refused at RCPT TO with 550 (user unknown)
  * an address containing "tempfail"  -> refused at RCPT TO with 451 (try again later)
  * a sender containing "unverified"  -> refused at MAIL FROM with 550 (sender not verified)
  * reject_data=True                  -> every message is refused after DATA with 554 (looks like spam)
  * auth=("user", "password")         -> the server asks for that login (AUTH PLAIN) before it accepts anything
  * everything else is accepted and remembered in .accepted
"""
import base64
import socketserver
import threading


class _Handler(socketserver.StreamRequestHandler):
    def reply(self, text):
        self.wfile.write((text + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        self.reply("220 fake-smtp ready")
        rcpts, sender, authed = [], "", self.server.auth is None
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            up = cmd.upper()
            if up.startswith("EHLO"):
                self.reply("250-fake-smtp\r\n250 AUTH PLAIN" if self.server.auth else "250 fake-smtp")
            elif up.startswith("HELO"):
                self.reply("250 fake-smtp")
            elif up.startswith("AUTH PLAIN"):
                try:
                    _, user, pw = base64.b64decode(cmd.split(None, 2)[2]).decode().split("\0")
                except Exception:       # noqa: BLE001
                    user = pw = None
                if (user, pw) == self.server.auth:
                    authed = True
                    self.reply("235 2.7.0 Authentication successful")
                else:
                    self.reply("535 5.7.8 Authentication credentials invalid")
            elif up.startswith("MAIL FROM"):
                sender, rcpts = cmd[10:].strip(), []
                if not authed:
                    self.reply("530 5.7.0 Authentication required")
                elif "unverified" in sender:
                    self.reply("550 5.7.1 Sender address is not verified")
                else:
                    self.reply("250 2.1.0 ok")
            elif up.startswith("RCPT TO"):
                addr = cmd[8:].strip().strip("<>")
                if "nobody" in addr:
                    self.reply(f"550 5.1.1 <{addr}>: Recipient address rejected: User unknown")
                elif "tempfail" in addr:
                    self.reply("451 4.3.0 Temporary problem, try again later")
                else:
                    rcpts.append(addr)
                    self.reply("250 2.1.5 ok")
            elif up == "DATA":
                if not rcpts:
                    self.reply("554 5.5.1 no valid recipients")
                    continue
                self.reply("354 end with <CRLF>.<CRLF>")
                data = []
                while True:
                    d = self.rfile.readline()
                    if d in (b".\r\n", b".\n", b""):
                        break
                    data.append(d)
                if self.server.reject_data:
                    self.reply("554 5.7.1 Message rejected: looks like spam")
                else:
                    self.server.accepted.append({"from": sender, "to": list(rcpts), "text": b"".join(data).decode("utf-8", "replace")})
                    self.reply("250 2.0.0 queued as FAKE123")
            elif up == "RSET":
                rcpts = []
                self.reply("250 ok")
            elif up == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("250 ok")


class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, auth=None, reject_data=False):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.auth, self.reject_data, self.accepted = auth, reject_data, []
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self):
        try:
            self.shutdown()
            self.server_close()
        except Exception:       # noqa: BLE001
            pass
