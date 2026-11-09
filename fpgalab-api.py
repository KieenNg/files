#!/usr/bin/env python3
"""fpgalab-api (version 1.7) - book FPGA Lab boards over the HTTP API.

User commands:
  fpgalab-api status                                 # list boards + departments
  fpgalab-api info <board>                           # one board in detail
  fpgalab-api signup                                 # ask for an account (an admin decides)
  fpgalab-api login                                  # prompts, saves a token
  fpgalab-api whoami                                 # who your token names
  fpgalab-api request <board> <duration> <dept> <purpose...>
      # duration: minutes (30/60/120/240/480) or the word  overnight
      # 30/60/120 on a free board start at once; 240/480 wait for an admin
  fpgalab-api queue <board> <duration> <dept> <purpose...>
      # join the line for a busy board; you are handed it in turn. Watch
      # `status` to see when it becomes yours (auto turns) or wait for an admin
      # to approve a 4h/8h turn.
  fpgalab-api leave <board>                          # give up your place in line
  fpgalab-api release <board>                        # give a board you hold back
  fpgalab-api requests                               # your requests waiting for an admin
  fpgalab-api cancel <id>                            # take one of them back
  fpgalab-api serial <board>                         # your board's UART settings
  fpgalab-api serial <board> <label> <baud> [8n1]    # change one UART's settings
  fpgalab-api console <board> <label> [<baud> [8n1]] # your board's UART, here
      # with <baud>: sets it first, as `serial` does (kept for the booking).
      # keys go straight to the UART (raw terminal); Ctrl-] quits. Linux/macOS.
      # exit 0 when you quit or the lab ends a session; 1 when it was refused.
  fpgalab-api log <board> [lines]                    # your board's activity log
  fpgalab-api stats [days]                           # usage statistics (JSON)
  fpgalab-api feedback <text...>                     # a note to the admins
  fpgalab-api password                               # change your password
  fpgalab-api update                                 # install the latest client
  fpgalab-api version                                # show version + build
  fpgalab-api help                                   # this text

Admin commands (an admin's token; lists print JSON):
  fpgalab-api pending                                # every request waiting
  fpgalab-api approve <id>                           # approve a request
  fpgalab-api reject <id> [reason...]                # reject a request
  fpgalab-api signups                                # account sign-ups waiting
  fpgalab-api approve-signup <id>
  fpgalab-api reject-signup <id> [reason...]
  fpgalab-api feedbacks                              # open feedback notes
  fpgalab-api resolve <id>                           # mark a note done
  fpgalab-api announce <text...>                     # the line everyone sees
  fpgalab-api announce --clear
  fpgalab-api unqueue <board> <account>              # take someone out of a line
  fpgalab-api release <board>                        # frees anyone's board
  fpgalab-api restart-hw                             # restart hw_server (asks first)
  `log` and `stats` cover every board and every user for an admin.

Not here: extending a booking (from a lab workstation, `fpgalab extend`).

Exit status: 0 done, 1 refused or failed, 2 unknown command or bad usage.
On every run it checks the server for a newer client and, if there is one, says
so; `fpgalab-api update` then downloads it and puts it in place of this file
(only a Python script that compiles and names its version; it is not run while
installing, and on any failure this file stays as it was). Set
FPGALAB_NOUPDATE=1 to skip that check. Server: FPGALAB_URL, default
mux://172.16.36.2:3120 - every request goes through the lab's mux port 3120,
the only port this client needs.
FPGALAB_URL=http://172.16.36.2:8080 talks to the web port directly instead;
`console` then still uses the mux, on the same host and port FPGALAB_MUX_PORT
(default 3120). Token saved to ~/.fpgalab-token.
Standard library only - copy this one file anywhere that reaches the lab network.

Your login token lasts 30 days, and ends sooner when your password changes or
the lab logs every client out (rare). After that just run  fpgalab-api login
once more.
"""
import json
import os
import sys
import getpass
import http.client
import re
import select
import signal
import socket
import stat
import urllib.request
import urllib.error
import urllib.parse

URL = os.environ.get("FPGALAB_URL", "mux://172.16.36.2:3120").rstrip("/")
_URL = urllib.parse.urlsplit(URL)
# mux://host:port -> (host, port): HTTP goes there, after a `MUX1 web` line.
MUX = (_URL.hostname, _URL.port or 3120) if _URL.scheme == "mux" else None
BASE = "http://%s:%d" % MUX if MUX else URL
# Where `console` opens a UART: the mux, on the API's host when it is http://.
MUX_ADDR = MUX or (_URL.hostname,
                   int(os.environ.get("FPGALAB_MUX_PORT") or 3120))
CTRL_RIGHT_BRACKET = b"\x1d"
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".fpgalab-token")
CLIENT_VERSION = "1.7"
CLIENT_BUILD = "66643daa0a79"


class _MuxConnection(http.client.HTTPConnection):
    """HTTP over the mux: the `web` route line first, then plain HTTP."""

    def connect(self):
        super().connect()
        self.sock.sendall(b"MUX1 web\n")


class _MuxHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_MuxConnection, req)


def urlopen(req, timeout):
    """urllib's urlopen, through the mux for a mux:// server (no proxy: the
    request must reach the mux itself)."""
    if MUX:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _MuxHandler).open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def unreachable(reason):
    if MUX:
        return ("Cannot reach the lab through %s:%d: %s\n"
                "Is port %d of the lab server open from here?"
                % (MUX[0], MUX[1], reason or "closed by the mux", MUX[1]))
    return ("Cannot reach %s: %s\n"
            "Are you on the lab network? Can you open the web page there?"
            % (BASE, reason))


def no_answer(reason):
    """The connection was made but closed without an HTTP answer."""
    return ("fpgalab-api: the lab at %s closed the connection without an"
            " answer (%s). Try again in a minute."
            % ("%s:%d" % MUX if MUX else BASE, reason or "closed"))


def call(method, path, body=None):
    """-> (HTTP status, JSON reply). A reply the user can only act on by
    reading it - a token that ended (401, reason auth), the lab's web down or
    busy (503) - is printed and exits 1; so does a lab that cannot be reached."""
    code, out = _call(method, path, body)
    if code == 503 or (code == 401 and out.get("reason") == "auth"):
        sys.exit("fpgalab-api: %s" % (out.get("error") or "HTTP %d" % code))
    return code, out


def _call(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urlopen(req, timeout=15) as r:
            raw = r.read() or b"{}"
            try:
                return r.status, json.loads(raw)
            except ValueError:
                # A 200 with a non-JSON body (e.g. a proxy's HTML error page):
                # report it cleanly instead of a raw traceback.
                return r.status, {"ok": False,
                                  "error": "Server did not return JSON"}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"ok": False, "error": "HTTP %d" % e.code}
    except urllib.error.URLError as e:
        sys.exit(unreachable(e.reason))
    except (OSError, http.client.HTTPException) as e:
        # Connected, then closed without an answer (an older mux with the
        # web down or its route full, which now answer 503), or broke mid-reply.
        sys.exit(no_answer(str(e)))


def show(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def read_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        sys.exit("Not logged in. Run: fpgalab-api login")


def maybe_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def with_token(path):
    """Append ?token= (if logged in) so reads show your own board's console."""
    tok = maybe_token()
    if not tok:
        return path
    sep = "&" if "?" in path else "?"
    return path + sep + "token=" + urllib.parse.quote(tok, safe="")


def newer():
    """The server's client version if its build differs from this one, else
    None. Any hiccup (offline, old server) counts as nothing newer."""
    try:
        with urlopen(BASE + "/api/client-version", timeout=4) as r:
            out = json.loads(r.read() or b"{}")
    except Exception:
        return None
    if not isinstance(out, dict):
        return None
    build = out.get("build")
    return out.get("version") if build and build != CLIENT_BUILD else None


def autocheck(argv):
    """Once per run: if the server has a newer client, say so on stderr. Never
    downloads or runs anything, and never blocks the real command."""
    if os.environ.get("FPGALAB_NOUPDATE"):
        return
    cmd = argv[0] if argv else ""
    if cmd in ("update", "version", "help", "-h", "--help"):
        return
    latest = newer()
    if latest:
        sys.stderr.write("fpgalab-api: a newer fpgalab-api (version %s) is"
                         " available: run fpgalab-api update\n" % latest)


class UpdateFailed(Exception):
    """Why `update` replaced nothing, in one line."""


def fetch(path):
    """GET `path` -> (body, its Content-Length or None); any failure is
    UpdateFailed."""
    where = "%s:%d" % MUX if MUX else BASE
    try:
        with urlopen(BASE + path, timeout=30) as r:
            return r.read(), r.headers.get("Content-Length")
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read() or b"{}")
        except ValueError:
            out = None
        why = out.get("error") if isinstance(out, dict) else None
        raise UpdateFailed(why or "HTTP %d" % e.code)
    except http.client.IncompleteRead:
        raise UpdateFailed("the download was cut short")
    except urllib.error.URLError as e:
        raise UpdateFailed("cannot reach the lab at %s: %s" % (where, e.reason))
    except (OSError, http.client.HTTPException) as e:
        raise UpdateFailed("the lab at %s closed the connection without an"
                           " answer (%s)" % (where, e or "closed"))


def check_client(body, length):
    """-> (version, build) of a downloaded client, else UpdateFailed: it must
    come with a well-formed Content-Length (`length`; http.client then reads
    exactly that many bytes or fails: "cut short"), be a Python
    script that compiles, end with this client's last line (a cut that still
    compiles does not), and name its version and build. It is compiled, never
    run."""
    if length is None:
        raise UpdateFailed("the download came with no Content-Length")
    if not re.fullmatch(r"[0-9]+", length.strip()):
        raise UpdateFailed("the download had a malformed Content-Length")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise UpdateFailed("the download is not text")
    if not text.strip():
        raise UpdateFailed("the download is empty")
    first = text.split("\n", 1)[0]
    if not (first.startswith("#!") and "python" in first):
        raise UpdateFailed("the download is not a Python script")
    try:
        compile(text, "fpgalab-api", "exec")
    except (SyntaxError, ValueError) as e:
        raise UpdateFailed("the download does not compile (%s)" % e)
    if not text.rstrip().endswith(
            'if __name__ == "__main__":\n    sys.exit(main(sys.argv[1:]))'):
        raise UpdateFailed("the download is incomplete (no end of the client)")
    found = [re.search(r'^%s = "([^"\n]+)"$' % name, text, re.M)
             for name in ("CLIENT_VERSION", "CLIENT_BUILD")]
    if not all(found):
        raise UpdateFailed("the download names no client version")
    return found[0].group(1), found[1].group(1)


def install(body):
    """Put `body` in place of this file: <file>.new beside it, with this
    file's mode (at least u+rx), fsync, then one os.replace - the file is the
    old client or the new one, never a part. A failure before the replace
    removes .new; a refused replace (on Windows a running file may be locked)
    leaves it, checked, for the user to move."""
    path = os.path.abspath(__file__)
    new = path + ".new"
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode) | 0o500
        with open(new, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(new, mode)
    except OSError as e:
        try:
            os.remove(new)
        except OSError:
            pass
        raise UpdateFailed("cannot write %s: %s" % (e.filename or new,
                                                    e.strerror or e))
    try:
        os.replace(new, path)
    except OSError as e:
        raise UpdateFailed("cannot replace %s (%s); the new client is %s -"
                           " on Windows a running file is locked: move it over"
                           " %s once this one has exited"
                           % (path, e.strerror or e, new, path))


def update():
    """Install the server's client in place of this file. -> exit status."""
    try:
        try:
            served = json.loads(fetch("/api/client-version")[0] or b"{}")
        except ValueError:
            served = None
        if not isinstance(served, dict):
            raise UpdateFailed("the lab's /api/client-version is not a JSON object")
        if served.get("build") == CLIENT_BUILD:
            print("fpgalab-api is up to date (version %s, build %s)"
                  % (CLIENT_VERSION, CLIENT_BUILD))
            return 0
        body, length = fetch("/fpgalab-api")
        version, build = check_client(body, length)
        install(body)
    except UpdateFailed as e:
        sys.stderr.write("fpgalab-api: update failed, nothing replaced: %s\n" % e)
        return 1
    print("updated fpgalab-api %s -> %s (build %s -> %s)"
          % (CLIENT_VERSION, version, CLIENT_BUILD, build))
    return 0


def _readline(sock, limit=1024):
    """One reply line from the mux, byte by byte: what follows is the UART's."""
    line = b""
    while not line.endswith(b"\n") and len(line) < limit:
        b = sock.recv(1)
        if not b:
            break
        line += b
    return line


STDOUT_CLOSED = ""     # our own output went away (`console ... | head`): quiet


def _relay(sock, termios, tty):
    """Keys -> UART, UART -> screen, until Ctrl-], the far side closes, or a
    signal. -> why it ended. The terminal is raw meanwhile and restored on
    every way out."""
    fd_in, fd_out = sys.stdin.fileno(), sys.stdout.fileno()
    saved = termios.tcgetattr(fd_in) if os.isatty(fd_in) else None

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    old = [(s, signal.signal(s, stop)) for s in (signal.SIGTERM, signal.SIGHUP)]
    try:
        if saved is not None:
            tty.setraw(fd_in)
        inputs = [fd_in, sock]
        try:
            while True:
                for ready in select.select(inputs, [], [])[0]:
                    if ready is sock:
                        data = sock.recv(65536)
                        if not data:
                            return "closed by the lab"
                        try:
                            while data:
                                data = data[os.write(fd_out, data):]
                        except BrokenPipeError:
                            return STDOUT_CLOSED
                        continue
                    data = os.read(fd_in, 4096)
                    if not data:                     # stdin ended (not a tty)
                        sock.shutdown(socket.SHUT_WR)
                        inputs.remove(fd_in)
                        continue
                    quit_at = data.find(CTRL_RIGHT_BRACKET)
                    keys = data if quit_at < 0 else data[:quit_at]
                    sock.sendall(keys)
                    if quit_at >= 0:
                        return "disconnected"
        except (ConnectionResetError, BrokenPipeError):
            # The lab's side reset the connection: a release or a restart
            # with our keys unread, a session replaced by a newer one. The
            # client cannot tell which, so it says no more than that.
            return "closed by the lab"
    finally:
        if saved is not None:
            termios.tcsetattr(fd_in, termios.TCSADRAIN, saved)
        for s, handler in old:
            signal.signal(s, handler)


def console(board, label):
    """The board's UART on this terminal, through the mux's `serial` route."""
    try:
        import termios
        import tty
    except ImportError:
        sys.stderr.write("fpgalab-api: console needs Linux/macOS for now\n")
        return 2
    tok = read_token()
    if len(tok.split()) != 1:
        sys.exit("The saved token is not valid. Run: fpgalab-api login")
    _, out = call("GET", "/api/info?board=%s&token=%s" % (
        urllib.parse.quote(board, safe=""), urllib.parse.quote(tok, safe="")))
    if not out.get("ok"):
        sys.exit("fpgalab-api: %s" % (out.get("error") or "no such board"))
    uart = [u for u in out["board"].get("uarts") or [] if u.get("label") == label]
    if not uart:
        sys.exit("fpgalab-api: %s has no UART %s" % (board, label))
    if not uart[0].get("mux_route"):
        sys.exit("fpgalab-api: %s %s is not open to you - book %s first"
                 " (fpgalab-api status shows who holds it), or your token has"
                 " ended: run fpgalab-api login" % (board, label, board))
    head = "MUX1 serial %s %s token=%s\n" % (board, label, tok)
    try:
        sock = socket.create_connection(MUX_ADDR, timeout=15)
    except OSError as e:
        sys.exit("Cannot reach the lab through %s:%d: %s" % (MUX_ADDR + (e,)))
    try:
        try:
            sock.sendall(head.encode("utf-8"))
            line = _readline(sock)
        except OSError as e:
            sys.exit("Cannot reach the lab through %s:%d: %s" % (MUX_ADDR + (e,)))
        if line != b"OK\n":
            text = line.decode("utf-8", "replace").strip().replace(tok, "***")
            sys.stderr.write("fpgalab-api: %s\n" % (text or "closed by the mux"))
            return 1
        sock.settimeout(None)
        sys.stdout.flush()
        sys.stderr.write("connected to %s %s - Ctrl-] to quit\n" % (board, label))
        sys.stderr.flush()
        why = _relay(sock, termios, tty)
    finally:
        sock.close()
    if why == STDOUT_CLOSED:
        return 0
    sys.stderr.write("\n%s\n" % why)
    return 0


def post(path, **body):
    """POST `body` with your token. -> the reply; a refusal is printed and
    exits 1."""
    body["token"] = read_token()
    _, out = call("POST", path, body)
    if not out.get("ok"):
        sys.exit("fpgalab-api: %s" % (out.get("error") or "refused"))
    return out


def usage(text):
    sys.stderr.write("Usage: fpgalab-api %s\n" % text)
    return 2


def ask_board(path, argv):
    """`request` / `queue`: the JSON reply, rc 1 when refused."""
    board, duration, dept = argv[1], argv[2], argv[3]
    _, out = call("POST", path, {
        "token": read_token(), "board": board, "department": dept,
        "purpose": " ".join(argv[4:]), "duration": duration,
    })
    show(out)
    return 0 if out.get("ok") else 1


def _mode_ok(mode):
    return len(mode) >= 3 and mode[0].isdigit() and mode[2:].isdigit()


def set_serial(board, label, baud, mode="8n1"):
    if not _mode_ok(mode):
        return usage("serial <board> <label> <baud> [<data><parity><stop>, e.g. 8n1]")
    post("/api/set-serial", board=board, ports={label: {
        "baud": baud, "data": mode[0], "parity": mode[1], "stop": mode[2:]}})
    print("%s %s: %s %s" % (board, label, baud, mode))
    return 0


def main(argv):
    autocheck(argv)
    cmd = argv[0] if argv else "status"
    n = len(argv)
    if cmd in ("status", "info"):
        if cmd == "info" and n < 2:
            return usage("info <board>")
        path = "/api/status" if cmd == "status" else (
            "/api/info?board=" + urllib.parse.quote(argv[1], safe=""))
        code, out = call("GET", with_token(path))
        show(out)
        return 0 if code == 200 and out.get("ok") else 1
    elif cmd == "login":
        name = input("Login: ").strip()
        pw = getpass.getpass("Password: ")
        _, out = call("POST", "/api/login", {"name": name, "password": pw})
        if not out.get("ok"):
            sys.exit("Login failed: " + str(out.get("error", "")))
        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            fh.write(out["token"])
        os.chmod(TOKEN_FILE, 0o600)
        print("Logged in as", out.get("name"), "- token saved to", TOKEN_FILE)
    elif cmd == "signup":
        name = input("Login you want (e.g. tuanm): ").strip()
        display = input("Full name (e.g. Nguyen Minh Tuan): ").strip()
        pw = getpass.getpass("Password: ")
        again = getpass.getpass("Password again: ")
        _, out = call("POST", "/api/signup", {
            "name": name, "display": display, "password": pw, "again": again})
        if not out.get("ok"):
            sys.exit("fpgalab-api: %s" % (out.get("error") or "refused"))
        print("Account %s requested. An admin will decide; you cannot log in"
              " until they do." % out.get("name"))
    elif cmd == "whoami":
        out = post("/api/whoami")
        print(out.get("name") + (" (admin)" if out.get("admin") else ""))
    elif cmd in ("request", "queue"):
        if n < 5:
            return usage("%s <board> <duration> <dept> <purpose...>" % cmd)
        return ask_board("/api/" + cmd, argv)
    elif cmd in ("leave", "unqueue"):
        if (cmd, n) not in (("leave", 2), ("unqueue", 3)):
            return usage("leave <board>" if cmd == "leave"
                         else "unqueue <board> <account>")
        body = {"token": read_token(), "board": argv[1]}
        if cmd == "unqueue":
            body["account"] = argv[2]
        _, out = call("POST", "/api/leave-queue", body)
        show(out)
        return 0 if out.get("ok") and out.get("left") is not False else 1
    elif cmd == "release":
        if n < 2:
            return usage("release <board>")
        _, out = call("POST", "/api/release",
                      {"token": read_token(), "board": argv[1]})
        show(out)
        return 0 if out.get("ok") else 1
    elif cmd == "requests":
        rows = post("/api/my-requests").get("requests") or []
        for r in rows:
            print("#%s %s %d min - %s (waiting for an admin; cancel: fpgalab-api"
                  " cancel %s)" % (r["id"], r["board"], r["minutes"],
                                   r.get("purpose") or "", r["id"]))
        if not rows:
            print("You have no requests waiting.")
    elif cmd == "cancel":
        if n != 2:
            return usage("cancel <id>")
        out = post("/api/cancel-request", id=argv[1])
        print("Request %s for %s cancelled." % (out.get("id"), out.get("board")))
    elif cmd == "serial":
        if n == 2:
            out = post("/api/serial", board=argv[1])
            for p in out.get("ports") or []:
                print("%s %s: %s %s%s%s" % (out.get("board"), p["label"], p["baud"],
                                            p["data"], p["parity"], p["stop"]))
        elif n in (4, 5):
            return set_serial(*argv[1:])
        else:
            return usage("serial <board> [<label> <baud> [8n1]]")
    elif cmd == "console":
        if n not in (3, 4, 5) or n == 5 and not _mode_ok(argv[4]):
            return usage("console <board> <label> [<baud> [8n1]]")
        if n > 3:
            # Unchanged settings are not sent: nothing to apply, and no audit
            # row for a change nobody made.
            mode = argv[4] if n == 5 else "8n1"
            want = argv[3] + mode
            now = post("/api/serial", board=argv[1], port=argv[2])
            have = ["%s%s%s%s" % (p["baud"], p["data"], p["parity"], p["stop"])
                    for p in now.get("ports") or [] if p.get("label") == argv[2]]
            if have != [want]:
                rc = set_serial(*argv[1:])
                if rc:
                    return rc
            else:
                print("%s %s: %s %s (unchanged)" % (argv[1], argv[2], argv[3], mode))
        return console(argv[1], argv[2])
    elif cmd == "log":
        if n not in (2, 3):
            return usage("log <board> [lines]")
        out = post("/api/board-log", board=argv[1],
                   lines=argv[2] if n == 3 else "")
        for line in out.get("lines") or []:
            print(line)
    elif cmd == "stats":
        show(post("/api/stats", days=argv[1] if n > 1 else ""))
    elif cmd == "feedback":
        if n < 2:
            return usage("feedback <text...>")
        post("/api/feedback", body=" ".join(argv[1:]))
        print("Thanks - your feedback reached the admins.")
    elif cmd == "password":
        old = getpass.getpass("Current password: ")
        new = getpass.getpass("New password: ")
        again = getpass.getpass("New password again: ")
        post("/api/password", old=old, new=new, again=again)
        print("Password changed. Your token has ended: run fpgalab-api login")
    elif cmd in ("pending", "signups", "feedbacks"):
        path = {"pending": "/api/requests", "signups": "/api/signups",
                "feedbacks": "/api/feedbacks"}[cmd]
        show(post(path))
    elif cmd in ("approve", "reject", "approve-signup", "reject-signup"):
        if n < 2 or (cmd.startswith("approve") and n != 2):
            return usage(cmd + (" <id>" if cmd.startswith("approve")
                                else " <id> [reason...]"))
        path = "/api/decide-signup" if cmd.endswith("signup") else "/api/decide"
        state = "approved" if cmd.startswith("approve") else "rejected"
        show(post(path, id=argv[1], state=state, reason=" ".join(argv[2:])))
    elif cmd == "resolve":
        if n != 2:
            return usage("resolve <id>")
        show(post("/api/resolve-feedback", id=argv[1]))
    elif cmd == "announce":
        if n < 2:
            return usage("announce <text...> | announce --clear")
        text = "" if argv[1:] == ["--clear"] else " ".join(argv[1:])
        show(post("/api/announcement", body=text))
    elif cmd == "restart-hw":
        word = input("This cuts every live JTAG session in the lab."
                     " Type restart to confirm: ").strip().lower()
        if word != "restart":
            sys.exit("fpgalab-api: not restarted")
        show(post("/api/restart-hw"))
    elif cmd == "update":
        if n != 1:
            return usage("update")
        return update()
    elif cmd == "version":
        print("fpgalab-api version %s (build %s)" % (CLIENT_VERSION, CLIENT_BUILD))
    elif cmd in ("help", "-h", "--help"):
        print(__doc__)
    else:
        sys.stderr.write("fpgalab-api: unknown command %r\n%s\n" % (cmd, __doc__))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
