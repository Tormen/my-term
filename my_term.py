#!/usr/bin/env -S python3 -I -S
"""my_term -- the one module that knows the terminal.

Which terminal it is, an image pasted into it, an image shown in it: for
iTerm2 (macOS) and Secure ShellFish (iOS, iPadOS), directly or inside tmux,
over ssh or not. A tool asks it by capability -- detect(), image_from(),
show() -- and never looks at a terminal itself, so a new terminal changes
this file alone.

Python 3.9 grammar and the standard library only. `python3 my_term.py -h`
says how to try it from a shell; `--run-tests [<filter>]` runs its tests.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import select
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, Final, Literal, Mapping, Sequence

VERSION: Final = "1.0"
NAME: Final = "my_term"

Kind = Literal["iterm2", "shellfish", "other"]
Run = Callable[[Sequence[str]], str]    # a command's words -> its stdout; OSError or CalledProcessError when it fails

ANSWER_WAIT: Final = 0.2                # seconds a terminal has to answer a question
XTVERSION: Final = b"\x1b[>q"           # "which terminal are you?" -- answered ESC P >| <name version> ESC \
DA1: Final = b"\x1b[c"                  # answered by every terminal: sent after a question, its answer ends the wait
DSR: Final = b"\x1b[6n"                 # "where is the cursor?" -- answered ESC [ <row> ; <column> R
XTVERSION_ANSWER: Final = re.compile(rb"\x1bP>\|([^\x1b]*)\x1b\\")
DA1_ANSWER: Final = re.compile(rb"\x1b\[\?[0-9;]*c")
DSR_ANSWER: Final = re.compile(rb"\x1b\[([0-9]+);([0-9]+)R")
TMUX_WHO: Final = "#{client_termtype}\t#{allow-passthrough}"
TMUX_WHERE: Final = "#{pane_top}\t#{pane_left}\t#{status}\t#{status-position}"
# the terminals seen to draw an image sent through tmux's passthrough (iTerm2 3.7.7, tmux 3.8); ShellFish's own
# page says images do not come through tmux, and no run has shown otherwise yet
DRAWS_THROUGH_TMUX: Final[frozenset[str]] = frozenset({"iterm2"})
KITTY_CHUNK: Final = 4096               # base64 bytes in one kitty graphics escape: the protocol's limit
IMAGE_SUFFIXES: Final = (".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".tiff", ".tif", ".bmp")
IMAGE_MAGIC: Final = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF87a", ".gif"),
                      (b"GIF89a", ".gif"))
BASE64_TEXT: Final = re.compile(r"[A-Za-z0-9+/=\s]+")
PASTED_PREFIX: Final = "pasted-"
BASE64_HINT: Final = "paste the image as base64 instead (iTerm2: Paste Base64-Encoded Contents)"


class Err(Exception):
    """A failure a person can act on: CODE names the line that raised it."""

    def __init__(self, code: str, text: str, rc: int = 1) -> None:
        super().__init__(f"{code}: {text}")
        self.code, self.text, self.rc = code, text, rc


# -------------------------------------------------------------------------
# which terminal
# -------------------------------------------------------------------------

@dataclass(frozen=True)
class Terminal:
    """The terminal a tool talks to, as detect() found it."""
    kind: Kind
    answer: str             # what named it: XTVERSION's answer, or tmux's client_termtype; "" when nothing did
    tmux: bool              # inside tmux ($TMUX)
    passthrough: bool       # tmux lets an escape sequence through to the terminal (allow-passthrough on|all)
    at_terminal: bool       # stdin and stdout are a terminal

    def draws(self) -> bool:
        """An image can be drawn here: a terminal that draws kitty's
        graphics protocol, reached directly, or through tmux's passthrough
        where that terminal is seen to draw through it."""
        if not self.at_terminal or self.kind == "other":
            return False
        return not self.tmux or (self.passthrough and self.kind in DRAWS_THROUGH_TMUX)

    def said(self) -> str:
        """One line for a tool's -V: what the terminal is, what named it,
        and whether images are drawn -- if not, why not."""
        if not self.at_terminal:
            named = "not a terminal"
        elif self.answer:
            named = f"{'tmux client_termtype' if self.tmux else 'XTVERSION'} {self.answer!r}"
        else:
            named = f"no answer in {round(ANSWER_WAIT * 1000)} ms" if not self.tmux else "tmux names no terminal"
        where = f", inside tmux (passthrough {'on' if self.passthrough else 'off'})" if self.tmux else ""
        if self.draws():
            how = "images drawn"
        elif self.at_terminal and self.kind != "other" and not self.passthrough:
            how = "images named by their file: tmux holds them back (set -g allow-passthrough on)"
        elif self.at_terminal and self.kind != "other":
            how = f"images named by their file: {self.kind} through tmux not yet seen to draw"
        else:
            how = "images named by their file"
        return f"terminal: {self.kind} ({named}){where} -- {how}"


def kind_of(answer: str) -> Kind:
    """The terminal ANSWER names: iTerm2's XTVERSION answer starts
    'iTerm2'; ShellFish's names it. Anything else -- tmux answering for
    itself among them -- is 'other'."""
    low = answer.strip().lower()
    if low.startswith("iterm2"):
        return "iterm2"
    if "shellfish" in low:
        return "shellfish"
    return "other"


def run_cmd(words: Sequence[str]) -> str:
    """WORDS run, their stdout: the Run every caller gets unless a test
    gives another. CalledProcessError when they fail."""
    return subprocess.run(list(words), check=True, capture_output=True, text=True, timeout=5).stdout


def ask(question: bytes, until: re.Pattern[bytes], fd_in: int = 0, fd_out: int = 1,
        wait: float = ANSWER_WAIT) -> bytes:
    """QUESTION written to the terminal (FD_OUT), its answer read from it
    (FD_IN) without echo, line editing or waiting for a line, until UNTIL
    matches what came or WAIT seconds pass: everything that came. The
    terminal's mode is put back however this ends; Ctrl-C still interrupts."""
    saved = termios.tcgetattr(fd_in)
    raw = termios.tcgetattr(fd_in)
    raw[3] &= ~(termios.ECHO | termios.ICANON)
    raw[6][termios.VMIN], raw[6][termios.VTIME] = 0, 0
    termios.tcsetattr(fd_in, termios.TCSANOW, raw)
    got = b""
    try:
        os.write(fd_out, question)
        end = time.monotonic() + wait
        while not until.search(got):
            left = end - time.monotonic()
            if left <= 0 or not select.select([fd_in], [], [], left)[0]:
                break
            chunk = os.read(fd_in, 1024)
            if not chunk:
                break
            got += chunk
    finally:
        termios.tcsetattr(fd_in, termios.TCSADRAIN, saved)
    return got


def detect(env: Mapping[str, str] | None = None, fd_in: int = 0, fd_out: int = 1, run: Run = run_cmd) -> Terminal:
    """The terminal on FD_IN and FD_OUT. Directly: its own answer to
    XTVERSION (it comes over ssh, where TERM_PROGRAM and LC_TERMINAL do
    not), DA1 sent after it so a terminal that does not know XTVERSION ends
    the wait at once. Inside tmux ($TMUX in ENV) tmux answers XTVERSION for
    itself, so tmux is asked instead: the terminal its client names
    (client_termtype) and whether passthrough is on. Not a terminal, or no
    answer: 'other'. Asked once per run: the answer costs up to ANSWER_WAIT."""
    env = os.environ if env is None else env
    tmux = bool(env.get("TMUX"))
    if not (os.isatty(fd_in) and os.isatty(fd_out)):
        return Terminal("other", "", tmux, False, False)
    if tmux:
        try:
            said = run(["tmux", "display-message", "-p", TMUX_WHO])
        except (OSError, subprocess.SubprocessError):
            said = ""
        answer, _, passthrough = said.rstrip("\n").partition("\t")
        return Terminal(kind_of(answer), answer, True, passthrough in ("on", "all"), True)
    m = XTVERSION_ANSWER.search(ask(XTVERSION + DA1, DA1_ANSWER, fd_in, fd_out))
    answer = m.group(1).decode("utf-8", "replace") if m else ""
    return Terminal(kind_of(answer), answer, False, False, True)


# -------------------------------------------------------------------------
# an image pasted
# -------------------------------------------------------------------------

@dataclass(frozen=True)
class Pasted:
    """An image a person pasted: HOW it came and the file it is."""
    how: Literal["file", "base64", "elsewhere"]
    path: str               # the image file on this host; for 'elsewhere' the path as pasted, which is not here
    said: str               # one line: how it came, or (elsewhere) what to do instead


def one_word(text: str) -> str | None:
    """TEXT as one shell word -- a dragged or pasted path, quoted or with
    a space escaped, a trailing space dropped -- else None."""
    try:
        words = shlex.split(text)
    except ValueError:
        return None
    return words[0] if len(words) == 1 else None


def image_suffix(data: bytes) -> str:
    """DATA's image type as a file suffix, by its first bytes: PNG, JPEG,
    GIF or WebP; "" when it is none of them."""
    for magic, suffix in IMAGE_MAGIC:
        if data.startswith(magic):
            return suffix
    return ".webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else ""


def keep_image(data: bytes, suffix: str, keep_dir: str) -> str:
    """DATA written into KEEP_DIR as pasted-<n><suffix>, n the first number
    no pasted image there has, whatever its type: written whole into a temp
    file first, then linked to its name, so no file is ever half written
    and none is overwritten. Its path."""
    os.makedirs(keep_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pasted.", dir=keep_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        n = 1
        while True:
            taken = {os.path.splitext(f)[0] for f in os.listdir(keep_dir)}
            if f"{PASTED_PREFIX}{n}" in taken:
                n += 1
                continue
            path = os.path.join(keep_dir, f"{PASTED_PREFIX}{n}{suffix}")
            try:
                os.link(tmp, path)
                return path
            except FileExistsError:
                n += 1
    finally:
        os.unlink(tmp)


def image_from(text: str, keep_dir: str) -> Pasted | None:
    """TEXT a person typed or pasted, as an image, else None (it is text).

    - a path naming a readable image file here: that file (ShellFish
      uploads a pasted image and types its path; a file dragged in);
    - an absolute image path that is not here: 'elsewhere' -- iTerm2's
      'Save to Temp File and Paste Path' over ssh types a path on the Mac;
    - base64 that decodes to a PNG, JPEG, GIF or WebP (iTerm2's 'Paste
      Base64-Encoded Contents', line breaks and all): written into KEEP_DIR
      (keep_image).
    Words that happen to be valid base64 ('done') decode to no image: text."""
    word = one_word(text)
    if word is not None:
        path = os.path.expanduser(word)
        if path.lower().endswith(IMAGE_SUFFIXES):
            if os.path.isfile(path) and os.access(path, os.R_OK):
                return Pasted("file", path, "")
            if os.path.isabs(path) and not os.path.lexists(path):
                host = socket.gethostname().split(".")[0]
                return Pasted("elsewhere", path, f"a path on another machine, not on {host} -- {BASE64_HINT}")
    if not BASE64_TEXT.fullmatch(text):
        return None
    try:
        data = base64.b64decode(re.sub(r"\s", "", text), validate=True)
    except (binascii.Error, ValueError):
        return None
    suffix = image_suffix(data)
    if not suffix:
        return None
    return Pasted("base64", keep_image(data, suffix, keep_dir), "pasted as base64")


# -------------------------------------------------------------------------
# an image shown
# -------------------------------------------------------------------------

def kitty(png: bytes, cols: int, rows: int, stay: bool = False) -> list[str]:
    """PNG as kitty graphics escapes, drawn at the cursor COLS cells wide
    and ROWS tall: the cursor then after the image, on its last row (STAY:
    where it was). One escape per KITTY_CHUNK of base64, every one but the
    last saying more follows; q=2: the terminal answers nothing."""
    data = base64.b64encode(png).decode("ascii")
    chunks = [data[i:i + KITTY_CHUNK] for i in range(0, len(data), KITTY_CHUNK)] or [""]
    keys = f"a=T,f=100,c={cols},r={rows},q=2" + (",C=1" if stay else "")
    return [f"\x1b_G{keys + ',' if k == 0 else ''}m={int(k < len(chunks) - 1)};{c}\x1b\\"
            for k, c in enumerate(chunks)]


def passthrough(seq: str) -> str:
    """SEQ wrapped for tmux to hand it to the terminal unread: its every
    ESC doubled, as tmux's DCS passthrough requires."""
    return "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def cursor(fd_in: int, fd_out: int) -> tuple[int, int] | None:
    """The cursor's row and column (from 1), as the terminal -- or the tmux
    pane -- answers DSR; None when it does not."""
    m = DSR_ANSWER.search(ask(DSR, DSR_ANSWER, fd_in, fd_out))
    return (int(m.group(1)), int(m.group(2))) if m else None


def show(term: Terminal, png: bytes, cols: int, rows: int, up: int, col: int, fd_in: int = 0, fd_out: int = 1,
         run: Run = run_cmd) -> bool:
    """PNG drawn COLS cells by ROWS over what is already on the screen, its
    top-left cell UP rows above the cursor's row, in column COL (from 1);
    the cursor stays where it is: True. The caller printed the text first,
    ROWS lines kept free where the image goes, so a terminal that cannot
    draw shows the text as it is. False when it cannot be drawn
    (term.draws(), or inside tmux the pane's place not found): nothing
    written -- the caller names the image's file.

    Inside tmux the image goes through passthrough, and the terminal would
    draw it wherever its own cursor is (the window's top-left), so it is
    placed: the pane's cursor (DSR, which tmux answers after everything
    written before it) plus the pane's place in the window (tmux
    display-message) gives the terminal's row and column. tmux does not
    know the image: it is gone when tmux redraws those cells."""
    if not term.draws():
        return False
    if not term.tmux:
        draw = ["\x1b7", f"\x1b[{up}A" if up else "", f"\x1b[{col}G", *kitty(png, cols, rows, stay=True), "\x1b8"]
        os.write(fd_out, "".join(draw).encode("ascii"))
        return True
    at = cursor(fd_in, fd_out)
    try:
        top, left, status, where = run(["tmux", "display-message", "-p", TMUX_WHERE]).rstrip("\n").split("\t")
        lines = 1 if status == "on" else int(status) if status.isdigit() else 0
        row, column = int(top) + (lines if where == "top" else 0), int(left)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if at is None:
        return False
    draw = ["\x1b7", f"\x1b[{row + at[0] - up};{column + col}H", *kitty(png, cols, rows, stay=True), "\x1b8"]
    os.write(fd_out, "".join(passthrough(s) for s in draw).encode("ascii"))
    return True


def png_size(png: bytes) -> tuple[int, int] | None:
    """A PNG's width and height in pixels, from its header; None: no PNG."""
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR" or len(png) < 24:
        return None
    w, h = struct.unpack(">II", png[16:24])
    return int(w), int(h)


# -------------------------------------------------------------------------
# the command line: try it from a shell
# -------------------------------------------------------------------------

USAGE: Final = f"usage: {NAME}.py [-h] [--version] [--run-tests [<filter>]] [kind | show <PNG>]"
HELP: Final = f"""{USAGE}

The one module that knows the terminal (iTerm2, Secure ShellFish; directly or
inside tmux). Tools import it; from a shell it shows what it finds:

  kind          the terminal, what named it, and whether images are drawn
  show <PNG>    draw the PNG at the cursor, {20} cells wide -- or say why not
  --run-tests [<filter>]   its tests (TAP), those whose title holds FILTER
  --version     the version, the git release when run in its own checkout,
                and the build (the file's own hash)
"""
SHOW_COLS: Final = 20


def version_line() -> str:
    """The version, git's describe when this file is the top of its own
    git checkout (a copy inside another repository reports none), and the
    build: the first 12 hex digits of the file's sha-256."""
    me = os.path.realpath(__file__)
    with open(me, "rb") as fh:
        build = hashlib.sha256(fh.read()).hexdigest()[:12]
    here = os.path.dirname(me)
    try:
        top = run_cmd(["git", "-C", here, "rev-parse", "--show-toplevel"]).strip()
        describe = run_cmd(["git", "-C", here, "describe", "--tags", "--long", "--always", "--dirty"]).strip() \
            if os.path.realpath(top) == here else ""
    except (OSError, subprocess.SubprocessError):
        describe = ""
    return f"{NAME} {VERSION} ({describe + ', ' if describe else ''}build {build})"


def main(argv: Sequence[str]) -> int:
    """The command line: rc 0, 1 an Err, 64 a usage error."""
    args = list(argv)
    if not args or args[0] in ("-h", "--help"):
        print(HELP, end="")
        return 0 if args else 64
    if args[0] == "--version":
        print(version_line())
        return 0
    if args[0] == "--run-tests":
        return run_tests(args[1] if len(args) > 1 else "")
    if args[0] == "kind" and len(args) == 1:
        print(detect().said())
        return 0
    if args[0] == "show" and len(args) == 2:
        try:
            with open(args[1], "rb") as fh:
                png = fh.read()
        except OSError as e:
            raise Err("001", f"{args[1]}: {e.strerror}") from e
        size = png_size(png)
        if size is None:
            raise Err("002", f"{args[1]}: not a PNG -- kitty's protocol is given PNGs (f=100)")
        term = detect()
        if not term.draws():
            print(f"{args[1]} -- not drawn: {term.said()}")
            return 0
        rows = max(round(SHOW_COLS * size[1] / max(size[0], 1) / 2), 1)   # a cell about twice as tall as wide
        print("\n" * (rows - 1), flush=True)                               # the rows it is drawn over
        if not show(term, png, SHOW_COLS, rows, rows, 1):
            print(f"{args[1]} -- not drawn: tmux did not say where the pane is")
        return 0
    raise Err("003", f"{' '.join(args)!r}: no such command -- {USAGE}", 64)


def entry() -> None:
    """main, with its failures as one line each: an Err its ERROR line,
    Ctrl-C one line and 130, anything else one line and 70, the trace in a
    file."""
    try:
        sys.exit(main(sys.argv[1:]))
    except Err as e:
        print(f"ERROR {e.code} [{e.rc}]: {e.text}", file=sys.stderr)
        sys.exit(e.rc)
    except KeyboardInterrupt:
        print(f"{NAME}: interrupted (Ctrl-C) -- nothing was changed", file=sys.stderr)
        sys.exit(130)
    except Exception:
        fd, trace = tempfile.mkstemp(prefix=f"{NAME}-trace.", suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            traceback.print_exc(file=fh)
        print(f"ERROR 004 [70]: {NAME} failed unexpectedly -- the trace is in {trace}", file=sys.stderr)
        sys.exit(70)


# -------------------------------------------------------------------------
# --run-tests: TAP, one line per test
# -------------------------------------------------------------------------

class T:
    """One test's checks and its scratch folder."""

    def __init__(self, tmp: str) -> None:
        self.tmp = tmp
        self.failed: list[str] = []

    def check(self, title: str, ok: bool, detail: str = "") -> None:
        if not ok:
            self.failed.append(f"{title}{' -- ' + detail if detail else ''}")


class FakeTerminal:
    """A terminal on the other side of a pty: answers each question it
    sees with the next of its ANSWERS for it, records all that was written."""

    def __init__(self, answers: Mapping[bytes, Sequence[bytes]]) -> None:
        self.master, self.slave = os.openpty()
        self.answers = {q: list(a) for q, a in answers.items()}
        self.seen = b""
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        pending = b""
        while not self.done.is_set():
            if not select.select([self.master], [], [], 0.02)[0]:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            self.seen += chunk
            pending += chunk
            for q, a in self.answers.items():
                while q in pending:
                    pending = pending.replace(q, b"", 1)
                    if a:
                        os.write(self.master, a.pop(0))

    def close(self) -> bytes:
        time.sleep(0.05)
        self.done.set()
        self.thread.join()
        os.close(self.slave)
        os.close(self.master)
        return self.seen


def fake_png(w: int = 2, h: int = 1) -> bytes:
    """A PNG's signature and header: enough for every check here."""
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\0\0\0"


def t_kind_of(t: T) -> None:
    t.check("iTerm2's answer", kind_of("iTerm2 3.7.7") == "iterm2")
    t.check("ShellFish's name", kind_of("Secure ShellFish 2026.35") == "shellfish")
    t.check("tmux answering for itself is no terminal of ours", kind_of("tmux 3.8") == "other")
    t.check("no answer", kind_of("") == "other")
    t.check("a name that merely holds 'iterm2' later", kind_of("xterm(iterm2)") == "other")


def t_detect_direct(t: T) -> None:
    fake = FakeTerminal({XTVERSION: [b"\x1bP>|iTerm2 3.7.7\x1b\\"], DA1: [b"\x1b[?62;4c"]})
    try:
        term = detect({}, fake.slave, fake.slave, run=lambda w: "")
    finally:
        fake.close()
    t.check("XTVERSION names iTerm2", term == Terminal("iterm2", "iTerm2 3.7.7", False, False, True), repr(term))
    t.check("drawn directly", term.draws())
    t.check("-V's line", term.said() == "terminal: iterm2 (XTVERSION 'iTerm2 3.7.7') -- images drawn", term.said())


def t_detect_no_xtversion(t: T) -> None:
    fake = FakeTerminal({DA1: [b"\x1b[?1;2c"]})
    try:
        start = time.monotonic()
        got = ask(XTVERSION + DA1, DA1_ANSWER, fake.slave, fake.slave, wait=5.0)
        took = time.monotonic() - start
    finally:
        fake.close()
    t.check("a terminal that only answers DA1 ends the wait at once, not after it", took < 2.0, f"{took:.2f}s")
    t.check("and names nothing", XTVERSION_ANSWER.search(got) is None, repr(got))


def t_detect_silent(t: T) -> None:
    fake = FakeTerminal({})
    try:
        start = time.monotonic()
        term = detect({}, fake.slave, fake.slave, run=lambda w: "")
        took = time.monotonic() - start
    finally:
        fake.close()
    t.check("a terminal that answers nothing: other", term.kind == "other" and term.at_terminal, repr(term))
    t.check("after the wait, not much longer", took < ANSWER_WAIT + 1.0, f"{took:.2f}s")
    t.check("said so", "no answer in 200 ms" in term.said(), term.said())


def t_detect_tmux(t: T) -> None:
    fake = FakeTerminal({})
    asked: list[Sequence[str]] = []

    def tmux(answer: str) -> Run:
        def run(words: Sequence[str]) -> str:
            asked.append(words)
            return answer
        return run
    try:
        it = detect({"TMUX": "/tmp/tmux-1/default,1,0"}, fake.slave, fake.slave, run=tmux("iTerm2 3.7.7\ton\n"))
        sf = detect({"TMUX": "x"}, fake.slave, fake.slave, run=tmux("ShellFish 2026.35\ton\n"))
        off = detect({"TMUX": "x"}, fake.slave, fake.slave, run=tmux("iTerm2 3.7.7\toff\n"))
    finally:
        seen = fake.close()
    t.check("inside tmux, tmux is asked -- never the pane, where tmux answers XTVERSION itself",
            XTVERSION not in seen and asked[0] == ["tmux", "display-message", "-p", TMUX_WHO], f"{seen!r} {asked}")
    t.check("iTerm2 through passthrough: drawn", it.kind == "iterm2" and it.passthrough and it.draws(), repr(it))
    t.check("ShellFish through tmux: not drawn, said why", sf.kind == "shellfish" and not sf.draws()
            and "shellfish through tmux not yet seen to draw" in sf.said(), sf.said())
    t.check("passthrough off: not drawn, said how to turn it on", not off.draws()
            and "set -g allow-passthrough on" in off.said(), off.said())


def t_detect_not_a_terminal(t: T) -> None:
    r, w = os.pipe()
    try:
        term = detect({}, r, w, run=lambda words: "iTerm2\ton")
    finally:
        os.close(r)
        os.close(w)
    t.check("a pipe: other, nothing asked", term == Terminal("other", "", False, False, False), repr(term))
    t.check("said so", "not a terminal" in term.said(), term.said())


def t_image_from_file(t: T) -> None:
    keep = os.path.join(t.tmp, "keep")
    png = os.path.join(t.tmp, "a shot.png")
    with open(png, "wb") as fh:
        fh.write(fake_png())
    t.check("a dragged path, quoted, a trailing space", image_from(shlex.quote(png) + " ", keep)
            == Pasted("file", png, ""))
    t.check("its space escaped", image_from(png.replace(" ", "\\ "), keep) == Pasted("file", png, ""))
    notes = os.path.join(t.tmp, "notes.txt")
    with open(notes, "w") as fh:
        fh.write("x")
    t.check("a file that is no image: text", image_from(notes, keep) is None)
    t.check("a relative image name not here: text", image_from("screenshot.png", keep) is None)
    t.check("nothing written", not os.path.exists(keep))


def t_image_from_elsewhere(t: T) -> None:
    got = image_from("/var/folders/_d/xyz/T/pasted-image-20261123-1310.png", t.tmp)
    t.check("a Mac's temp path, not here: elsewhere, with what to do", got is not None and got.how == "elsewhere"
            and BASE64_HINT in got.said, repr(got))


def t_image_from_base64(t: T) -> None:
    keep = os.path.join(t.tmp, "keep")
    png = fake_png(812, 430) + b"\0" * 300
    wrapped = base64.b64encode(png).decode()
    wrapped = "\r".join(wrapped[i:i + 76] for i in range(0, len(wrapped), 76))      # iTerm2 breaks it with CR
    first = image_from(wrapped, keep)
    second = image_from(base64.b64encode(b"GIF89a" + b"\0" * 40).decode(), keep)
    t.check("a PNG as wrapped base64: kept as pasted-1.png, its bytes whole", first is not None
            and first.how == "base64" and first.path == os.path.join(keep, "pasted-1.png")
            and open(first.path, "rb").read() == png, repr(first))
    t.check("the next one beside it, nothing overwritten", second is not None
            and second.path == os.path.join(keep, "pasted-2.gif"), repr(second))
    t.check("no temp file left", sorted(os.listdir(keep)) == ["pasted-1.png", "pasted-2.gif"], repr(os.listdir(keep)))


def t_image_from_words(t: T) -> None:
    keep = os.path.join(t.tmp, "keep")
    for words in ("done", "TODO", "yes", "abcd1234", base64.b64encode(b"hello, a text").decode(), "a sentence"):
        t.check(f"{words!r}: valid base64 or not, no image -- text", image_from(words, keep) is None)
    t.check("nothing written", not os.path.exists(keep))


def t_image_suffix(t: T) -> None:
    t.check("JPEG", image_suffix(b"\xff\xd8\xff\xe0rest") == ".jpg")
    t.check("WebP", image_suffix(b"RIFF\0\0\0\0WEBPVP8 ") == ".webp")
    t.check("a RIFF that is no WebP (a WAV)", image_suffix(b"RIFF\0\0\0\0WAVEfmt ") == "")


def t_kitty(t: T) -> None:
    png = bytes(range(256)) * 40                    # 10 KB: three chunks of base64
    parts = kitty(png, 20, 5)
    payload = "".join(p.split(";", 1)[1][:-2] for p in parts)
    t.check("chunks of at most 4096", all(len(p.split(";", 1)[1]) - 2 <= KITTY_CHUNK for p in parts)
            and len(parts) == 4, f"{len(parts)} parts")
    t.check("the keys on the first only, m=1 but on the last", parts[0].startswith("\x1b_Ga=T,f=100,c=20,r=5,q=2,m=1;")
            and parts[1].startswith("\x1b_Gm=1;") and parts[-1].startswith("\x1b_Gm=0;"), repr([p[:30] for p in parts]))
    t.check("the chunks together are the PNG", base64.b64decode(payload) == png)
    t.check("stay: C=1", kitty(b"x", 1, 1, stay=True)[0].startswith("\x1b_Ga=T,f=100,c=1,r=1,q=2,C=1,m=0;"))


def t_show_direct(t: T) -> None:
    term = Terminal("iterm2", "iTerm2 3.7.7", False, False, True)
    fake = FakeTerminal({})
    try:
        ok = show(term, b"png", 4, 2, 5, 9, fake.slave, fake.slave)
    finally:
        seen = fake.close().decode("ascii")
    t.check("drawn 5 rows up in column 9, the cursor put back, nothing else written",
            ok and seen == "\x1b7\x1b[5A\x1b[9G" + "".join(kitty(b"png", 4, 2, stay=True)) + "\x1b8", repr(seen))
    fake = FakeTerminal({})
    try:
        no = show(Terminal("other", "", False, False, True), b"png", 4, 2, 5, 9, fake.slave, fake.slave)
    finally:
        seen = fake.close().decode("ascii")
    t.check("a terminal that draws nothing: False, nothing written", not no and seen == "", repr(seen))


def t_show_tmux(t: T) -> None:
    term = Terminal("iterm2", "iTerm2 3.7.7", True, True, True)
    fake = FakeTerminal({DSR: [b"\x1b[20;5R"]})
    asked: list[Sequence[str]] = []

    def run(words: Sequence[str]) -> str:
        asked.append(words)
        return "2\t40\ton\ttop\n"
    try:
        ok = show(term, b"png", 4, 4, 6, 9, fake.slave, fake.slave, run)
    finally:
        seen = fake.close().decode("ascii")
    pt = re.findall(r"\x1bPtmux;((?:[^\x1b]|\x1b\x1b)*)\x1b\\", seen)
    inner = [p.replace("\x1b\x1b", "\x1b") for p in pt]
    t.check("drawn", ok, repr(seen))
    t.check("the pane asked where its cursor is (DSR), nothing written to the pane besides",
            seen.startswith("\x1b[6n\x1bPtmux;") and re.sub(r"\x1bPtmux;(?:[^\x1b]|\x1b\x1b)*\x1b\\", "", seen)
            == "\x1b[6n", repr(seen[:40]))
    t.check("placed in the window: row 2 + 1 (the status line on top) + 20 (the pane's cursor) - 6 up, "
            "column 40 + 9", inner[:2] == ["\x1b7", "\x1b[17;49H"] and inner[-1] == "\x1b8", repr(inner))
    t.check("the image itself, cursor kept, every ESC doubled for tmux", inner[2:-1] == kitty(b"png", 4, 4, stay=True)
            and all(re.fullmatch(r"(?:[^\x1b]|\x1b\x1b)*", p) for p in pt), repr(pt))
    t.check("tmux asked for the pane's place", asked == [["tmux", "display-message", "-p", TMUX_WHERE]], repr(asked))


def t_show_tmux_no_place(t: T) -> None:
    term = Terminal("iterm2", "iTerm2 3.7.7", True, True, True)
    fake = FakeTerminal({})                         # the pane never answers DSR

    def run(words: Sequence[str]) -> str:
        raise subprocess.CalledProcessError(1, list(words))
    try:
        ok = show(term, b"png", 4, 2, 2, 1, fake.slave, fake.slave, run)
    finally:
        seen = fake.close()
    t.check("no cursor answer: False, nothing drawn", not ok and b"\x1bPtmux;" not in seen, repr(seen))


def t_cli(t: T) -> None:
    me = os.path.realpath(__file__)

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", "-S", me, *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=30)
    v = cli("--version")
    t.check("--version: name, version, build", v.returncode == 0
            and re.fullmatch(rf"{NAME} {VERSION} \((.+, )?build [0-9a-f]{{12}}\)\n", v.stdout) is not None, v.stdout)
    bad = cli("frobnicate")
    t.check("an unknown command: its ERROR line on stderr, rc 64, no trace", bad.returncode == 64 and bad.stdout == ""
            and bad.stderr.startswith("ERROR 003 [64]: 'frobnicate': no such command") and "Traceback" not in bad.stderr,
            bad.stderr)
    png = os.path.join(t.tmp, "x.png")
    with open(png, "wb") as fh:
        fh.write(fake_png())
    k = cli("show", png)
    t.check("show, not at a terminal: says why it is not drawn", k.returncode == 0
            and "not drawn: terminal: other (not a terminal)" in k.stdout, k.stdout + k.stderr)
    txt = os.path.join(t.tmp, "x.txt")
    with open(txt, "w") as fh:
        fh.write("x")
    n = cli("show", txt)
    t.check("show of a file that is no PNG: ERROR 002", n.returncode == 1 and "ERROR 002 [1]:" in n.stderr, n.stderr)


def t_guard(t: T) -> None:
    defined = {n for n in globals() if n.startswith("t_")}
    listed = {f.__name__ for _, f in TESTS}
    t.check("every test function is in the run list, and only those", defined == listed, repr(defined ^ listed))


TESTS: Final[Sequence[tuple[str, Callable[[T], None]]]] = (
    ("kind: XTVERSION's answer names the terminal", t_kind_of),
    ("detect: directly, by XTVERSION", t_detect_direct),
    ("detect: a terminal without XTVERSION ends the wait at DA1", t_detect_no_xtversion),
    ("detect: a silent terminal is other, after the wait", t_detect_silent),
    ("detect: inside tmux, tmux is asked", t_detect_tmux),
    ("detect: not a terminal", t_detect_not_a_terminal),
    ("paste: a path to an image file here", t_image_from_file),
    ("paste: a path from another machine", t_image_from_elsewhere),
    ("paste: base64 becomes a kept file", t_image_from_base64),
    ("paste: words are text, base64 or not", t_image_from_words),
    ("paste: image types by their bytes", t_image_suffix),
    ("show: kitty escapes, chunked", t_kitty),
    ("show: directly", t_show_direct),
    ("show: inside tmux, placed through passthrough", t_show_tmux),
    ("show: inside tmux, no place found", t_show_tmux_no_place),
    ("cli: --version, errors, show", t_cli),
    ("guard: the run list", t_guard),
)


def run_tests(pattern: str) -> int:
    """The tests whose title holds PATTERN, TAP: the plan, then ok / not ok
    per test as it ends, each failed check under it. rc 0 when all passed."""
    chosen = [(title, f) for title, f in TESTS if pattern in title]
    print(f"1..{len(chosen)}", flush=True)
    failed = 0
    for k, (title, f) in enumerate(chosen, 1):
        tmp = tempfile.mkdtemp(prefix=f"{NAME}-test.")
        t = T(tmp)
        try:
            f(t)
        except Exception as e:  # a test that raises has failed; the run goes on
            t.failed.append(f"raised {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"{'not ok' if t.failed else 'ok'} {k} - {title}", flush=True)
        for line in t.failed:
            print(f"#   FAILED: {line}", flush=True)
        failed += bool(t.failed)
    print(f"# {len(chosen) - failed}/{len(chosen)} ok" + (f", {failed} FAILED" if failed else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    entry()
