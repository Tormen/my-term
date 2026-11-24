#!/usr/bin/env -S python3 -I -S
"""my_term -- the one module that knows the terminal.

Which terminal it is, an image pasted into it, an image shown in it, a
key prompt, the window's width, a busy line: for iTerm2 (macOS) and Secure
ShellFish (iOS, iPadOS), directly or inside tmux, over ssh or not. A tool
asks it by capability -- detect(), image_from(), show(), Prompt, widen(),
Busy -- and never
looks at a terminal itself, so a new terminal changes this file alone.

Python 3.9 grammar and the standard library only. `python3 my_term.py -h`
says how to try it from a shell; `--run-tests [<filter>]` runs its tests.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import io
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
import unicodedata
from dataclasses import dataclass
from typing import Callable, Final, Literal, Mapping, Sequence, TextIO

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
CELL_SIZE_ANSWER: Final = re.compile(rb"\x1b\[6;([0-9]+);([0-9]+)t")
# any of the answers above: a CSI ending R, c or t (no key a terminal sends ends so), or a DCS
REPLY: Final = re.compile(rb"\x1b\[[?>]?[0-9;]*[Rct]|\x1bP[^\x1b]*\x1b\\")
TYPED_AHEAD: Final = bytearray()        # input that came while ask() waited for a reply: Prompt reads it first
TMUX_CELL_SIZE: Final = "#{window_cell_height}\t#{window_cell_width}"
TMUX_WHO: Final = "#{client_termtype}\t#{allow-passthrough}"
TMUX_WHERE: Final = "#{pane_top}\t#{pane_left}\t#{status}\t#{status-position}"
TMUX_WIDTHS: Final = "#{client_width}\t#{pane_width}"
WIDEN_WAIT: Final = 1.0                 # seconds a terminal has to widen its window
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
    matches what came or WAIT seconds pass: the terminal's replies that
    came. What the user typed meanwhile is no reply: it is kept in
    TYPED_AHEAD, where Prompt reads it first. The terminal's mode is put
    back however this ends; Ctrl-C still interrupts."""
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
    TYPED_AHEAD.extend(REPLY.sub(b"", got))
    return b"".join(m.group(0) for m in REPLY.finditer(got))


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


def cell_size(env: Mapping[str, str] | None = None, fd_in: int = 0, fd_out: int = 1,
              run: Run = run_cmd) -> tuple[int, int] | None:
    """Cell height and width in pixels, or None when unknown.

    tmux's window reports its cell dimensions; directly, CSI 16 t asks the
    terminal, with DA1 ending the wait when the terminal does not answer."""
    env = os.environ if env is None else env
    if not (os.isatty(fd_in) and os.isatty(fd_out)):
        return None
    if env.get("TMUX"):
        try:
            parts = run(["tmux", "display-message", "-p", TMUX_CELL_SIZE]).strip().split("\t")
            if len(parts) != 2:
                return None
            height, width = (int(part) for part in parts)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    else:
        match = CELL_SIZE_ANSWER.search(ask(b"\x1b[16t" + DA1, DA1_ANSWER, fd_in, fd_out))
        if match is None:
            return None
        height, width = int(match.group(1)), int(match.group(2))
    return (height, width) if height > 0 and width > 0 else None


# -------------------------------------------------------------------------
# a prompt's key line and input
# -------------------------------------------------------------------------

KEY_ESC: Final = "⎋"                   # the keys that type no character, by the Mac's signs for them, as the
KEY_RETURN: Final = "↩"                # arrows are ↑ ↓ → ←: a Prompt offers and returns them like any key
KEY_PGUP: Final = "⇞"
KEY_PGDN: Final = "⇟"
KEY_HOME: Final = "↖"
KEY_END: Final = "↘"
KEY_LABELS: Final[Mapping[str, str]] = {
    "y": "yes", "n": "no", "o": "ok", "w": "write", "?": "unclear",
    "s": "skip", "@": "later or agent", "u": "undo", "e": "edit",
    "v": "view", "c": "cycle", "C": "cancel", "Q": "quit", "A": "apply",
    KEY_ESC: "back",
}
KEY_RESIZED: Final = "\x00resize"
ESC_WAIT: Final = 0.05                  # seconds after a lone ESC with nothing behind it: the Esc key itself
SEQUENCES: Final[Mapping[bytes, str]] = {
    b"\x1b[A": "↑", b"\x1b[B": "↓", b"\x1b[C": "→", b"\x1b[D": "←",
    b"\x1bOA": "↑", b"\x1bOB": "↓", b"\x1bOC": "→", b"\x1bOD": "←",
    b"\x1b[5~": KEY_PGUP, b"\x1b[6~": KEY_PGDN,
    b"\x1b[H": KEY_HOME, b"\x1bOH": KEY_HOME, b"\x1b[1~": KEY_HOME, b"\x1b[7~": KEY_HOME,
    b"\x1b[F": KEY_END, b"\x1bOF": KEY_END, b"\x1b[4~": KEY_END, b"\x1b[8~": KEY_END,
}


def cells(text: str) -> int:
    """The terminal cells TEXT takes: a wide character two, a combining one none."""
    return sum(0 if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf")
               else 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def keys_text(keys: Sequence[str]) -> str:
    """KEYS as a group of them is drawn: a run of three or more consecutive
    characters as its first and last ('1-3', 'a-z'), any other key alone,
    all apart by a space: 'B D E', 'a-z 1-9'."""
    runs: list[list[str]] = []
    for key in keys:
        if runs and ord(key) == ord(runs[-1][-1]) + 1:
            runs[-1].append(key)
        else:
            runs.append([key])
    return " ".join(f"{run[0]}-{run[-1]}" if len(run) >= 3 else " ".join(run) for run in runs)


class Prompt:
    """A key line and its input: only the offered keys are returned.

    Each key is one character; the keys that type none are KEY_ESC,
    KEY_RETURN, KEY_PGUP, KEY_PGDN, KEY_HOME, KEY_END and the arrows
    ↑ ↓ → ←. LABELS names every key KEY_LABELS does not; keys given the
    same label are drawn as one group, '[1-3] as similar'. A SUBMENU -- a
    prompt entered from another -- offers its keys and KEY_ESC (back) and
    nothing else, so its labels may name house keys too. Reuse the prompt
    across timed reads so an incomplete escape or UTF-8 character remains
    pending. Ctrl-C raises KeyboardInterrupt; Ctrl-D and the end of the
    input raise EOFError.

    It reads STDIN and draws on STDOUT, text streams -- sys.stdin and
    sys.stdout as they are at each call, unless given -- so the key line
    lands in order with the tool's own lines on that stream, and a stream a
    test put in their place (io.StringIO) is read and written like any.
    """

    def __init__(self, keys: Sequence[str], labels: Mapping[str, str] | None = None,
                 stdin: TextIO | None = None, stdout: TextIO | None = None, *, mark: Mapping[str, str] | None = None,
                 quiet: Sequence[str] = (), style: str = "", width: int | None = None,
                 wrap_before: str = "", indent: int = 0, submenu: bool = False) -> None:
        extra = {} if labels is None else dict(labels)
        marks = {} if mark is None else dict(mark)
        keys = (*keys, KEY_ESC) if submenu and KEY_ESC not in keys else tuple(keys)
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("keys must be nonempty and unique")
        if any(len(k) != 1 or not k.isprintable() for k in keys):
            raise ValueError("each key must be one printable character")
        if set(extra) - set(keys):
            raise ValueError("labels may name only offered keys")
        if not submenu and set(extra) & set(KEY_LABELS):
            raise ValueError("a house key keeps its shared label outside a submenu")
        if any(not label or not label.isprintable() for label in extra.values()):
            raise ValueError("labels must be printable and nonempty")
        if any(k not in KEY_LABELS and k not in extra for k in keys):
            raise ValueError("each key needs a shared or caller label")
        self.space_is_view = "v" in keys and "v" not in extra
        if self.space_is_view and " " in keys:
            raise ValueError("space is already the alias for view")
        if set(quiet) - set(keys) or set(marks) - (set(keys) - set(quiet)):
            raise ValueError("quiet keys must be offered and marks must name visible keys")
        if any(not text or not text.isprintable() for text in marks.values()):
            raise ValueError("marks must be printable and nonempty")
        if wrap_before and (wrap_before not in keys or wrap_before in quiet):
            raise ValueError("wrap_before must name a visible key")
        if width is not None and width < 1:
            raise ValueError("width must be positive")
        if indent < 0:
            raise ValueError("indent must not be negative")
        if style and re.fullmatch(r"[0-9]+(?:;[0-9]+)*", style) is None:
            raise ValueError("style must be SGR numbers")
        self.keys = frozenset(keys)
        visible = [k for k in keys if k not in quiet]
        self.parts: list[tuple[frozenset[str], str]] = []     # the keys a part of the line draws, and its text
        drawn: set[str] = set()
        for k in visible:
            if k in drawn:
                continue
            group = [m for m in visible if m in extra and extra[m] == extra[k]] if k in extra else [k]
            drawn.update(group)
            if len(group) > 1:
                if set(group) & set(marks):
                    raise ValueError("a mark must name a key drawn alone")
                self.parts.append((frozenset(group), f"[{keys_text(group)}] {extra[k]}"))
            else:
                text = self._label(k, extra[k] if k in extra else KEY_LABELS[k])
                text += (" (space)" if k == "v" and self.space_is_view else "") + (" " + marks[k] if k in marks else "")
                self.parts.append((frozenset(group), text))
        self.style, self.width, self.wrap_before, self.indent = style, width, wrap_before, indent
        self.stdin, self.stdout = stdin, stdout
        self.pending = bytearray()

    def _in(self) -> TextIO:
        return sys.stdin if self.stdin is None else self.stdin

    def _out(self) -> TextIO:
        return sys.stdout if self.stdout is None else self.stdout

    @staticmethod
    def _label(key: str, label: str) -> str:
        return f"[{key}]{label[1:]}" if label.startswith(key) else f"[{key}] {label}"

    def draw(self) -> None:
        """Draw the offered keys; read(draw=False) can follow other output.

        A line wider than WIDTH (the terminal's, unless given) is broken
        between keys -- before WRAP_BEFORE first -- and every row after the
        first starts INDENT columns in."""
        try:
            width = self.width or os.get_terminal_size(self._out().fileno()).columns or 80   # 0: a size nobody set
        except (OSError, ValueError):                                   # ValueError: a stream with no fd
            width = 80
        whole = " ".join(text for _, text in self.parts) + ": "
        rows: list[str] = []
        if cells(whole) <= width:
            rows.append(whole)
        else:
            row = ""
            for i, (keys, text) in enumerate(self.parts):
                text += ": " if i == len(self.parts) - 1 else ""
                room = width - (self.indent if rows else 0)
                if row and (self.wrap_before in keys or cells(f"{row} {text}") > room):
                    rows.append(row)
                    row = text
                else:
                    row = f"{row} {text}" if row else text
            rows.append(row)
        line = ("\n" + " " * self.indent).join(rows)
        if self.style:
            line = f"\x1b[{self.style}m{line}\x1b[0m"
        self._out().write(line)
        self._out().flush()

    def discard(self) -> None:
        """Discard typed-ahead input before showing a new question. Input
        that is no terminal keeps its lines: they are the answers."""
        if self._in().isatty():
            self.pending.clear()
            TYPED_AHEAD.clear()
            termios.tcflush(self._in().fileno(), termios.TCIFLUSH)

    def _byte(self, wait: float | None) -> bytes | None:
        """The next byte of input: what a question to the terminal left
        typed ahead first. None when none came within WAIT seconds (None:
        no limit); b"" at the end of the input."""
        if TYPED_AHEAD:
            byte = bytes(TYPED_AHEAD[:1])
            del TYPED_AHEAD[:1]
            return byte
        fd = self._in().fileno()
        if not select.select([fd], [], [], wait)[0]:
            return None
        return os.read(fd, 1)

    def _key(self, byte: bytes) -> tuple[str, str] | None:
        """BYTE from a terminal added to what is pending: the key it
        completes ('' for none known) and the text it came as; None while it
        is incomplete. A lone ESC is the Esc key when nothing follows it
        within ESC_WAIT."""
        if not byte:
            raise EOFError("terminal input closed")
        if byte in (b"\x03", b"\x04"):
            self.pending.clear()
            if byte == b"\x03":
                raise KeyboardInterrupt
            raise EOFError("Ctrl-D")
        self.pending.extend(byte)
        if self.pending[0] == 27:
            if len(self.pending) == 1:
                follows = self._byte(ESC_WAIT)
                if follows:
                    return self._key(follows)
                key = KEY_ESC
            elif self.pending[1] in (ord("["), ord("O")) and (len(self.pending) < 3 or
                                                               not 0x40 <= self.pending[-1] <= 0x7e):
                return None
            else:
                key = SEQUENCES.get(bytes(self.pending), "")
        elif bytes(self.pending) in (b"\r", b"\n"):
            key = KEY_RETURN
        else:
            try:
                key = self.pending.decode("utf-8")
            except UnicodeDecodeError as e:
                if e.reason == "unexpected end of data" and len(self.pending) < 4:
                    return None
                key = ""
        said = bytes(self.pending).decode("utf-8", "replace")
        self.pending.clear()
        return key, said

    def _line(self) -> tuple[str, str]:
        """The next line of input that is no terminal: its first character
        as the key -- an empty line KEY_RETURN, ESC KEY_ESC -- and the line.
        At the end of the input EOFError."""
        line = self._in().readline()
        if not line:
            raise EOFError("input closed")
        line = line.rstrip("\n").rstrip("\r")
        first = line[:1]
        return {"": KEY_RETURN, "\x1b": KEY_ESC}.get(first, first), line

    def read(self, wait: float = 0, draw: bool = True, resized: Callable[[], bool] | None = None,
             refused: Callable[[str], None] | None = None) -> str | None:
        """Draw the line and return an offered key, or None after WAIT seconds.

        WAIT zero waits indefinitely; DRAW false continues a timed wait
        without repeating the line. RESIZED consumes resize notifications;
        REFUSED receives each unoffered key, or it rings the bell. Input
        that is no terminal (a pipe, a file, a StringIO) gives a key per
        line, the line's first character; there a read waits for a line,
        whatever WAIT says. The terminal mode is restored on every return
        or exception.
        """
        if wait < 0:
            raise ValueError("wait must not be negative")
        tty = self._in().isatty()
        fd = self._in().fileno() if tty else -1
        saved = termios.tcgetattr(fd) if tty else None
        if saved is not None:
            raw = termios.tcgetattr(fd)
            raw[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
            raw[6][termios.VMIN], raw[6][termios.VTIME] = 1, 0
            termios.tcsetattr(fd, termios.TCSANOW, raw)
        try:
            if draw:
                self.draw()
            end = time.monotonic() + wait
            while True:
                if resized is not None and resized():
                    return KEY_RESIZED
                if not tty:
                    got = self._line()
                else:
                    left = max(0.0, end - time.monotonic()) if wait else None
                    if resized is not None:
                        left = min(left, 0.1) if left is not None else 0.1
                    byte = self._byte(left)
                    if byte is None:
                        if resized is not None and (not wait or time.monotonic() < end):
                            continue
                        return None
                    got = self._key(byte)
                    if got is None:
                        continue
                key, said = got
                if key == " " and self.space_is_view:
                    return "v"
                if key in self.keys:
                    return key
                if refused is None:
                    self._out().write("\a")
                    self._out().flush()
                else:
                    refused(key or said)
        finally:
            if saved is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)


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


def widen(term: Terminal, cols: int, fd_out: int = 1, run: Run = run_cmd, wait: float = WIDEN_WAIT) -> bool:
    """The window made at least COLS columns wide when it is narrower, and
    left so: True when FD_OUT now has COLS columns or more. The terminal is
    asked by xterm's window operation CSI 8 ; ; <columns> t, its rows kept;
    inside tmux through passthrough, the window widened by what the pane
    lacks. Then the size is watched for up to WAIT seconds. A terminal that
    does not obey, tmux without passthrough, or no terminal: False -- the
    caller says what it needed."""
    try:
        now = os.get_terminal_size(fd_out).columns
    except OSError:
        return False
    if now >= cols:
        return True
    if not term.at_terminal or (term.tmux and not term.passthrough):
        return False
    window = cols
    if term.tmux:
        try:
            client, pane = (int(n) for n in run(["tmux", "display-message", "-p", TMUX_WIDTHS]).split("\t"))
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        window = client + cols - pane
    seq = f"\x1b[8;;{window}t"
    os.write(fd_out, (passthrough(seq) if term.tmux else seq).encode("ascii"))
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.02)
        if os.get_terminal_size(fd_out).columns >= cols:
            return True
    return False


def png_size(png: bytes) -> tuple[int, int] | None:
    """A PNG's width and height in pixels, from its header; None: no PNG."""
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR" or len(png) < 24:
        return None
    w, h = struct.unpack(">II", png[16:24])
    return int(w), int(h)


# -------------------------------------------------------------------------
# slow work: a busy line
# -------------------------------------------------------------------------

BUSY_CELLS: Final = 8                   # the scanner's width
BUSY_TICK: Final = 0.08                 # seconds between two of its frames
BUSY_SECONDS_FROM: Final = 1.0          # the seconds since the start shown from then on
SCANNER_TAIL: Final = "▓▒░"             # behind the head, nearest first


def scanner(tick: int, dim: bool = True) -> str:
    """The scanner at frame TICK: a head █ running back and forth across
    BUSY_CELLS, its tail SCANNER_TAIL behind it -- dim, when DIM."""
    span = BUSY_CELLS - 1
    pos = tick % (2 * span)
    forward = pos < span
    head = pos if forward else 2 * span - pos
    row = [" "] * BUSY_CELLS
    for i, ch in enumerate(SCANNER_TAIL, 1):
        at = head - i if forward else head + i
        if 0 <= at < BUSY_CELLS:
            row[at] = f"\x1b[2m{ch}\x1b[22m" if dim else ch
    row[head] = "█"
    return "".join(row)


def cut(text: str, room: int) -> str:
    """TEXT in at most ROOM cells: cut, with … at its end, when longer."""
    if cells(text) <= room:
        return text
    while text and cells(text) > room - 1:
        text = text[:-1]
    return text + "…" if room > 0 else ""


class Busy:
    """A line saying what slow work runs, around it: `with Busy(text) as b:`.

    On a terminal TEXT shows with a scanner moving beside it, and after
    BUSY_SECONDS_FROM the seconds since the start, redrawn in place every
    BUSY_TICK by a thread of its own. When the block ends, the line becomes
    done()'s result and the time it took (`#45 sent [rc 0], 61s`), else
    `<text> -- done, 61s`. If the block raises -- Ctrl-C among it -- the
    line is cleared and the exception goes on, so the tool's own ERROR or
    interrupt line stands alone. Not a terminal: nothing while it runs, the
    result line at the end. It writes STDOUT, sys.stdout unless given."""

    def __init__(self, text: str, stdout: TextIO | None = None) -> None:
        self.text = text
        self.out = sys.stdout if stdout is None else stdout
        self.live = self.out.isatty()
        self.result: str | None = None
        self.start = time.monotonic()
        self.tick = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> Busy:
        self.start = time.monotonic()
        if self.live:
            self._draw()
            self.thread.start()
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None, trace: object) -> None:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()
        took = f"{int(time.monotonic() - self.start)}s"
        line = "" if kind is not None else (f"{self.result}, {took}" if self.result is not None
                                            else f"{self.text} -- done, {took}")
        with self.lock:
            self.out.write(("\r\x1b[K" if self.live else "") + (line + "\n" if line else ""))
            self.out.flush()

    def done(self, result: str) -> None:
        """The line that replaces the busy one when the block ends: RESULT and the time it took."""
        self.result = result

    def say(self, line: str) -> None:
        """LINE printed above the busy one, which goes on below it: output while the work runs."""
        with self.lock:
            self.out.write(("\r\x1b[K" if self.live else "") + line + "\n")
            if self.live:
                self.out.write(self._frame())
            self.out.flush()

    def _run(self) -> None:
        while not self.stop.wait(BUSY_TICK):
            self.tick += 1
            self._draw()

    def _frame(self) -> str:
        try:
            width = os.get_terminal_size(self.out.fileno()).columns or 80
        except (OSError, ValueError):
            width = 80
        took = time.monotonic() - self.start
        seconds = f"  {int(took)}s" if took >= BUSY_SECONDS_FROM else ""
        room = width - 1 - (BUSY_CELLS + 4) - len(seconds)
        return f"\r{cut(self.text, room)}  [{scanner(self.tick)}]{seconds}\x1b[K"

    def _draw(self) -> None:
        with self.lock:
            if not self.stop.is_set():
                self.out.write(self._frame())
                self.out.flush()


# -------------------------------------------------------------------------
# the command line: try it from a shell
# -------------------------------------------------------------------------

USAGE: Final = f"usage: {NAME}.py [-h] [--version] [--run-tests [<filter>]] [kind | show <PNG> | widen <COLUMNS> |\n" \
                    "                  busy <SECONDS>]"
HELP: Final = f"""{USAGE}

The one module that knows the terminal (iTerm2, Secure ShellFish; directly or
inside tmux). Tools import it; from a shell it shows what it finds:

  kind          the terminal, what named it, and whether images are drawn
  show <PNG>    draw the PNG at the cursor, {20} cells wide -- or say why not
  widen <COLUMNS>   ask the terminal for a window that wide; rc 0 when it is
  busy <SECONDS>    the busy line with its scanner for that long, then its result
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
    if args[0] == "busy" and len(args) == 2 and args[1].isdigit():
        with Busy(f"busy {args[1]}"):
            time.sleep(int(args[1]))
        return 0
    if args[0] == "widen" and len(args) == 2 and args[1].isdigit():
        try:
            was = os.get_terminal_size(1).columns
        except OSError:
            raise Err("003", "widen: standard output is no terminal") from None
        term = detect()
        fits = widen(term, int(args[1]))
        now = os.get_terminal_size(1).columns
        print(f"widen {args[1]}: {now} columns (was {was}) -- " + ("fits" if fits else f"too narrow; {term.said()}"))
        return 0 if fits else 1
    raise Err("004", f"{' '.join(args)!r}: no such command -- {USAGE}", 64)


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
        print(f"ERROR 005 [70]: {NAME} failed unexpectedly -- the trace is in {trace}", file=sys.stderr)
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

    def streams(self) -> tuple[TextIO, TextIO]:
        """Text streams on the slave, as a tool's sys.stdin and sys.stdout are on a terminal."""
        return (open(self.slave, encoding="utf-8", closefd=False),
                open(self.slave, "w", encoding="utf-8", closefd=False))

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


def t_cell_size(t: T) -> None:
    fake = FakeTerminal({b"\x1b[16t": [b"\x1b[6;21;9t"], DA1: [b"\x1b[?1;2c"]})
    asked: list[Sequence[str]] = []

    def tmux_run(words: Sequence[str]) -> str:
        asked.append(words)
        return "21\t9\n"

    try:
        direct = cell_size({}, fake.slave, fake.slave)
        tmux = cell_size({"TMUX": "x"}, fake.slave, fake.slave, run=tmux_run)
        zero = cell_size({"TMUX": "x"}, fake.slave, fake.slave, run=lambda words: "0\t0\n")
        malformed = cell_size({"TMUX": "x"}, fake.slave, fake.slave, run=lambda words: "oops")
    finally:
        fake.close()
    t.check("direct answer: height and width", direct == (21, 9), repr(direct))
    t.check("tmux window cell size", tmux == (21, 9), repr(tmux))
    t.check("tmux is asked for window dimensions", asked == [["tmux", "display-message", "-p", TMUX_CELL_SIZE]],
            repr(asked))
    t.check("zero is unknown", zero is None, repr(zero))
    t.check("invalid is unknown", malformed is None, repr(malformed))
    silent = FakeTerminal({DA1: [b"\x1b[?1;2c"]})
    try:
        unknown = cell_size({}, silent.slave, silent.slave)
    finally:
        silent.close()
    t.check("unsupported query is unknown", unknown is None, repr(unknown))
    r, w = os.pipe()
    try:
        pipe = cell_size({}, r, w)
    finally:
        os.close(r)
        os.close(w)
    t.check("a pipe is unknown", pipe is None, repr(pipe))


def t_prompt(t: T) -> None:
    fake = FakeTerminal({})
    saved = termios.tcgetattr(fake.slave)
    raw = termios.tcgetattr(fake.slave)
    raw[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    termios.tcsetattr(fake.slave, termios.TCSANOW, raw)
    try:
        prompt = Prompt(("y", "n", "C", "r"), {"r": "run"}, *fake.streams())
        os.write(fake.master, b"xyC")
        first = prompt.read(wait=1)
        second = prompt.read(wait=1)
        timeout = prompt.read(wait=0.05)
        os.write(fake.master, b"\x03")
        try:
            prompt.read(wait=1)
            interrupted = False
        except KeyboardInterrupt:
            interrupted = True
    finally:
        termios.tcsetattr(fake.slave, termios.TCSANOW, saved)
        shown = fake.close()
    t.check("invalid x refused; y and C kept in order", (first, second) == ("y", "C"), repr((first, second)))
    t.check("timeout and Ctrl-C", timeout is None and interrupted)
    t.check("line uses shared and own labels", b"[y]es" in shown and b"[r]un" in shown, repr(shown))
    for keys, labels in ((('y',), {'y': 'override'}), (('x',), {}), (('x',), {'z': 'other'})):
        try:
            Prompt(keys, labels)
        except ValueError:
            continue
        t.check("bad key or override refused", False, repr((keys, labels)))


def t_prompt_walk(t: T) -> None:
    fake = FakeTerminal({})
    saved = termios.tcgetattr(fake.slave)
    raw = termios.tcgetattr(fake.slave)
    raw[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    termios.tcsetattr(fake.slave, termios.TCSANOW, raw)
    rejected: list[str] = []
    try:
        prompt = Prompt(("y", "@", "g"), {"g": "go"}, *fake.streams(),
                        mark={"y": "✅ (recommended)"}, quiet="g", style="2", width=20, wrap_before="@")
        os.write(fake.master, b"xg")
        got = prompt.read(wait=1, refused=rejected.append)
    finally:
        termios.tcsetattr(fake.slave, termios.TCSANOW, saved)
        shown = fake.close().decode("utf-8")
    t.check("mark and wrap before at, dimmed", shown == "\x1b[2m[y]es ✅ (recommended)\r\n[@] later or agent: \x1b[0m",
            repr(shown))
    t.check("quiet key accepted and not shown", got == "g" and "[g]" not in shown, repr(got))
    t.check("refused key passed to callback", rejected == ["x"], repr(rejected))
    out = io.StringIO()
    Prompt(("y", "@"), stdout=out, width=80, wrap_before="@").draw()
    t.check("wide terminal keeps one line", out.getvalue() == "[y]es [@] later or agent: ", repr(out.getvalue()))
    bad: Sequence[Callable[[], Prompt]] = (lambda: Prompt(("y",), mark={"x": "unexpected"}),
                                           lambda: Prompt(("y",), quiet="x"),
                                           lambda: Prompt(("y",), style="2m"),
                                           lambda: Prompt(("y",), wrap_before="x"))
    for create in bad:
        try:
            create()
        except ValueError:
            continue
        t.check("invalid prompt option refused", False)


def t_prompt_controls(t: T) -> None:
    fake = FakeTerminal({})
    saved = termios.tcgetattr(fake.slave)
    raw = termios.tcgetattr(fake.slave)
    raw[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    termios.tcsetattr(fake.slave, termios.TCSANOW, raw)
    try:
        view = Prompt(("v", "y"), None, *fake.streams())
        os.write(fake.master, b" ")
        alias = view.read(wait=1)
        os.write(fake.master, b"yy")
        view.discard()
        discarded = view.read(wait=0.05)
        resized = view.read(wait=1, resized=lambda: True)
        space = Prompt((" ",), {" ": "preview"}, *fake.streams())
        os.write(fake.master, b" ")
        own = space.read(wait=1)
        no_view = Prompt(("y",), None, *fake.streams())
        os.write(fake.master, b" y")
        refused = no_view.read(wait=1)
        notice = threading.Event()
        timer = threading.Timer(0.02, notice.set)
        timer.start()
        delayed = no_view.read(wait=1, resized=notice.is_set)
        timer.join()
        write = Prompt(("w",), None, *fake.streams())
        os.write(fake.master, b"whello\n")
        selected = write.read(wait=1)
        text_after_key = os.read(fake.slave, 6)
    finally:
        termios.tcsetattr(fake.slave, termios.TCSANOW, saved)
        fake.close()
    t.check("space aliases view only when offered", alias == "v" and own == " " and refused == "y",
            repr((alias, own, refused)))
    t.check("discard removes typed-ahead keys", discarded is None, repr(discarded))
    t.check("resize is distinct from timeout", resized == delayed == KEY_RESIZED, repr((resized, delayed)))
    t.check("following text stays for the line editor", (selected, text_after_key) == ("w", b"hello\n"),
            repr((selected, text_after_key)))


def t_prompt_fragments(t: T) -> None:
    fake = FakeTerminal({})
    saved = termios.tcgetattr(fake.slave)
    raw = termios.tcgetattr(fake.slave)
    raw[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    termios.tcsetattr(fake.slave, termios.TCSANOW, raw)
    before_mode = termios.tcgetattr(fake.slave)
    try:
        p = Prompt(("y", "→"), {"→": "next"}, *fake.streams())
        os.write(fake.master, b"\x1b[")
        before = p.read(wait=0.05)
        os.write(fake.master, b"Cy")
        arrow, yes = p.read(wait=1), p.read(wait=1)
        os.write(fake.master, b"\x1b[5~y")
        ignored = p.read(wait=1)
        restored = termios.tcgetattr(fake.slave) == before_mode
    finally:
        termios.tcsetattr(fake.slave, termios.TCSANOW, saved)
        fake.close()
    t.check("split arrow survives timeout and preserves the next key", (before, arrow, yes) == (None, "→", "y"),
            repr((before, arrow, yes)))
    t.check("unknown escape does not become a key", ignored == "y", repr(ignored))
    t.check("terminal mode restored", restored)


def raw_pty() -> tuple[FakeTerminal, list[int | list[int | bytes]]]:
    """A fake terminal whose slave is in the mode Prompt reads in, and the mode it had."""
    fake = FakeTerminal({})
    saved = termios.tcgetattr(fake.slave)
    raw = termios.tcgetattr(fake.slave)
    raw[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    termios.tcsetattr(fake.slave, termios.TCSANOW, raw)
    return fake, saved


def t_prompt_keys(t: T) -> None:
    fake, saved = raw_pty()
    got: list[str | None] = []
    try:
        p = Prompt((KEY_ESC, KEY_RETURN, KEY_PGUP, KEY_PGDN, KEY_HOME, KEY_END, "↑", "C"),
                   {KEY_RETURN: "keep", KEY_PGUP: "page up", KEY_PGDN: "page down", KEY_HOME: "first",
                    KEY_END: "last", "↑": "up"}, *fake.streams())
        os.write(fake.master, b"\x1b")
        got.append(p.read(wait=1))
        os.write(fake.master, b"\x1b[AC\r\x1b[5~\x1b[6~\x1b[H\x1b[F\x1bOH\x1b[4~")
        got += [p.read(wait=1) for _ in range(9)]
        os.write(fake.master, b"\x04")
        try:
            p.read(wait=1)
            eof = False
        except EOFError:
            eof = True
    finally:
        termios.tcsetattr(fake.slave, termios.TCSANOW, saved)
        shown = fake.close().decode("utf-8")
    t.check("Esc alone is the Esc key", got[0] == KEY_ESC, repr(got[:1]))
    t.check("an arrow sent at once stays an arrow, not Esc and text", got[1:3] == ["↑", "C"], repr(got[1:3]))
    t.check("Return, PgUp, PgDn, Home, End in their spellings",
            got[3:] == [KEY_RETURN, KEY_PGUP, KEY_PGDN, KEY_HOME, KEY_END, KEY_HOME, KEY_END], repr(got[3:]))
    t.check("Ctrl-D raises EOFError", eof)
    t.check("Esc drawn with its shared label", "[⎋] back" in shown and "[↩] keep" in shown, repr(shown[:80]))


def t_prompt_pipe(t: T) -> None:
    out = io.StringIO()
    rejected: list[str] = []
    p = Prompt(("y", KEY_RETURN, KEY_ESC, "C"), {KEY_RETURN: "keep"}, io.StringIO("yes\n\nxz\n\x1b\nC"), out)
    p.discard()
    got = [p.read(refused=rejected.append) for _ in range(4)]
    try:
        p.read()
        eof = False
    except EOFError:
        eof = True
    t.check("a line's first character; an empty line Return; ESC Esc; a last line without newline",
            got == ["y", KEY_RETURN, KEY_ESC, "C"], repr(got))
    t.check("an unoffered line refused, discard keeps the lines", rejected == ["x"], repr(rejected))
    t.check("the end of the input raises EOFError", eof)
    t.check("the line is drawn for each read", out.getvalue().count("[y]es") == 5, repr(out.getvalue()))
    r, w = os.pipe()
    try:
        os.write(w, b"C\n")
        os.close(w)
        with open(r, encoding="utf-8", closefd=False) as piped:
            from_pipe = Prompt(("C",), None, piped, io.StringIO()).read()
    finally:
        os.close(r)
    t.check("a pipe read the same way", from_pipe == "C", repr(from_pipe))


def t_prompt_streams(t: T) -> None:
    was = sys.stdin, sys.stdout
    try:
        p = Prompt(("y", "n"))                       # sys.stdin and sys.stdout as they are when it reads
        sys.stdin, sys.stdout = io.StringIO("n\n"), io.StringIO()
        print("delete the 3 copies?", end=" ")
        got = p.read()
        shown = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = was
    t.check("keys from the sys.stdin a test put in place", got == "n", repr(got))
    t.check("the key line after the tool's own text on the same stream",
            shown == "delete the 3 copies? [y]es [n]o: ", repr(shown))


def t_prompt_groups(t: T) -> None:
    alike, letters = io.StringIO(), io.StringIO()
    Prompt(("B", "D", "E", "1", "2", "3", "s"), {"B": "folder", "D": "folder", "E": "folder", "1": "as similar",
           "2": "as similar", "3": "as similar"}, stdout=alike, width=100).draw()
    look = "abcdefghijklmnopqrstuvwxyz123456789"
    Prompt(tuple(look), dict.fromkeys(look, "Quick Look"), stdout=letters, width=100, submenu=True).draw()
    t.check("one label for keys apart and for a range",
            alike.getvalue() == "[B D E] folder [1-3] as similar [s]kip: ", repr(alike.getvalue()))
    t.check("letters and digits as two ranges", letters.getvalue() == "[a-z 1-9] Quick Look [⎋] back: ",
            repr(letters.getvalue()))
    try:
        Prompt(("1", "2"), {"1": "x", "2": "x"}, mark={"1": "!"})
        t.check("a mark on a grouped key refused", False)
    except ValueError:
        pass


def t_prompt_indent(t: T) -> None:
    out = io.StringIO()
    Prompt(("y", "n", "s", "u", "Q"), stdout=out, width=16, indent=4).draw()
    t.check("broken between keys, each row after the first indented, none wider than the width",
            out.getvalue() == "[y]es [n]o\n    [s]kip\n    [u]ndo\n    [Q] quit: ", repr(out.getvalue()))


def t_prompt_submenu(t: T) -> None:
    fake, saved = raw_pty()
    try:
        p = Prompt(("f", "c", "y", "n"), {"f": "folder", "c": "country", "y": "type", "n": "name"},
                   *fake.streams(), submenu=True)
        os.write(fake.master, b"Qn")
        name = p.read(wait=1)
        os.write(fake.master, b"\x1b")
        back = p.read(wait=1)
    finally:
        termios.tcsetattr(fake.slave, termios.TCSANOW, saved)
        shown = fake.close().decode("utf-8")
    t.check("own labels on house letters, Esc offered as back",
            shown.startswith("[f]older [c]ountry [y] type [n]ame [⎋] back: "), repr(shown[:60]))
    t.check("a house key not listed is refused; Esc returns", (name, back) == ("n", KEY_ESC), repr((name, back)))
    try:
        Prompt(("c",), {"c": "country"})
        t.check("own label on a house key outside a submenu refused", False)
    except ValueError:
        pass


def t_typed_ahead(t: T) -> None:
    fake = FakeTerminal({DSR: [b"\x1b[5;1R"]})
    saved = termios.tcgetattr(fake.slave)
    try:
        TYPED_AHEAD.clear()
        os.write(fake.master, b"\x1b[")
        time.sleep(0.05)
        at = cursor(fake.slave, fake.slave)
        os.write(fake.master, b"C")
        p = Prompt(("→", "C"), {"→": "next"}, *fake.streams())
        key = p.read(wait=1)
    finally:
        termios.tcsetattr(fake.slave, termios.TCSANOW, saved)
        TYPED_AHEAD.clear()
        fake.close()
    t.check("the cursor still read", at == (5, 1), repr(at))
    t.check("an arrow cut by the reply is still the arrow, not a plain C", key == "→", repr(key))


def t_widen(t: T) -> None:
    fake = FakeTerminal({})
    size = struct.pack("HHHH", 24, 80, 0, 0)
    fcntl.ioctl(fake.slave, termios.TIOCSWINSZ, size)
    direct = Terminal("iterm2", "iTerm2", False, False, True)
    try:
        def obey() -> None:
            end = time.monotonic() + 1
            while b"\x1b[8;;100t" not in fake.seen and time.monotonic() < end:
                time.sleep(0.01)
            fcntl.ioctl(fake.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        helper = threading.Thread(target=obey)
        helper.start()
        widened = widen(direct, 100, fake.slave)
        helper.join()
        before = len(fake.seen)
        wide = widen(direct, 90, fake.slave)
        time.sleep(0.05)
        asked_again = len(fake.seen) != before
        stubborn = widen(direct, 120, fake.slave, wait=0.1)
        no_pass = widen(Terminal("iterm2", "iTerm2", True, False, True), 130, fake.slave, wait=0.1)
        widths: list[Sequence[str]] = []

        def tmux(words: Sequence[str]) -> str:
            widths.append(words)
            return "150\t100\n"
        in_tmux = widen(Terminal("iterm2", "iTerm2", True, True, True), 140, fake.slave, run=tmux, wait=0.1)
    finally:
        seen = fake.close()
    t.check("a terminal that obeys: True at the new width", widened, repr(seen[-40:]))
    t.check("wide enough already: True, nothing asked", wide and not asked_again)
    t.check("a terminal that does not obey: False", not stubborn)
    t.check("tmux without passthrough: False, nothing sent", not no_pass and b"130t" not in seen)
    t.check("inside tmux: the window widened by what the pane lacks, through passthrough",
            not in_tmux and passthrough("\x1b[8;;190t").encode() in seen
            and widths == [["tmux", "display-message", "-p", TMUX_WIDTHS]], repr((widths, seen[-40:])))


def frames(seen: bytes) -> list[str]:
    """What a busy line drew, one frame each: its \\r-separated parts, SGR and erase removed."""
    text = re.sub(r"\x1b\[[0-9;]*[mK]", "", seen.decode("utf-8", "replace"))
    return [f for f in text.split("\r") if f]


def t_busy_scanner(t: T) -> None:
    got = [scanner(k, dim=False) for k in (0, 3, 7, 8, 10, 14)]
    t.check("the head runs right with its tail behind, turns, runs back",
            got == ["█       ", "░▒▓█    ", "       █", "      █▓", "    █▓▒░", "█       "], repr(got))
    t.check("the tail dim", scanner(3).startswith("\x1b[2m░\x1b[22m"), repr(scanner(3)))


def t_busy_terminal(t: T) -> None:
    fake = FakeTerminal({})
    fcntl.ioctl(fake.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 30, 0, 0))
    _, out = fake.streams()
    try:
        with Busy("sending your 1 answer to ada", out) as b:
            time.sleep(0.3)
            b.say("a note")
            b.start -= 5
            time.sleep(0.2)
            b.done("#45 sent [rc 0]")
        Busy("waiting", out).say("x")                 # no thread to draw it again: say itself does
    finally:
        seen = fake.close()
    drawn = frames(seen)
    moving = [f for f in drawn if "[" in f and "█" in f]
    t.check("the line redrawn in place, the scanner moving", len(moving) >= 4 and len(set(moving)) >= 3,
            repr(drawn[:6]))
    t.check("no frame wider than the window less one", all(cells(f.rstrip("\n")) <= 29 for f in moving),
            repr([f for f in moving if cells(f) > 29][:2]))
    t.check("the text cut to fit", any(f.startswith("sending your 1 a…  [") for f in moving), repr(moving[:2]))
    t.check("the seconds once past the first", any(f.endswith("  5s") for f in moving), repr(moving[-2:]))
    t.check("say: its line above, the busy line again below", "\r\x1b[Ka note\r\n\r" in seen.decode(), repr(seen[-200:]))
    t.check("the result line with the time it took, in place of the busy one",
            "\r\x1b[K#45 sent [rc 0], 5s\r\n".encode() in seen, repr(seen[-60:]))
    t.check("say draws the busy line again itself", "\r\x1b[Kx\r\n\rwaiting  [".encode() in seen, repr(seen[-60:]))


def t_busy_not_a_terminal(t: T) -> None:
    out = io.StringIO()
    with Busy("sending", out) as b:
        b.say("a note")
        b.done("#45 sent [rc 0]")
    plain = io.StringIO()
    with Busy("rebuilding the index", plain):
        pass
    t.check("nothing moves; say's line and the result line", out.getvalue() == "a note\n#45 sent [rc 0], 0s\n",
            repr(out.getvalue()))
    t.check("without done(): the text, done", plain.getvalue() == "rebuilding the index -- done, 0s\n",
            repr(plain.getvalue()))


def t_busy_raises(t: T) -> None:
    out = io.StringIO()
    try:
        with Busy("sending", out):
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        interrupted = True
    else:
        interrupted = False
    fake = FakeTerminal({})
    _, tty = fake.streams()
    try:
        try:
            with Busy("sending", tty):
                time.sleep(0.1)
                raise ValueError("x")
        except ValueError:
            pass
    finally:
        seen = fake.close()
    t.check("Ctrl-C goes on, nothing printed where nothing moved", interrupted and out.getvalue() == "",
            repr(out.getvalue()))
    t.check("on a terminal the line cleared, no result line", seen.endswith(b"\r\x1b[K") and b"done" not in seen,
            repr(seen[-40:]))


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
            and bad.stderr.startswith("ERROR 004 [64]: 'frobnicate': no such command") and "Traceback" not in bad.stderr,
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
    ("prompt: key line and input", t_prompt),
    ("prompt: walk marks quiet keys and layout", t_prompt_walk),
    ("prompt: alias discard and resize", t_prompt_controls),
    ("prompt: split input and terminal mode", t_prompt_fragments),
    ("prompt: Esc, Return, Ctrl-D, page keys", t_prompt_keys),
    ("prompt: input that is no terminal, a key per line", t_prompt_pipe),
    ("prompt: sys.stdin and sys.stdout as text streams, replaced by a test", t_prompt_streams),
    ("prompt: one label for a group of keys", t_prompt_groups),
    ("prompt: a wrapped line indented", t_prompt_indent),
    ("prompt: a submenu offers its keys and Esc", t_prompt_submenu),
    ("prompt: keys typed while the terminal answers are kept", t_typed_ahead),
    ("widen: the window asked for more columns", t_widen),
    ("busy: the scanner runs back and forth", t_busy_scanner),
    ("busy: on a terminal, a line in place, then the result", t_busy_terminal),
    ("busy: not a terminal, the result line alone", t_busy_not_a_terminal),
    ("busy: the block raises, the line cleared", t_busy_raises),
    ("cell size: direct and tmux", t_cell_size),
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
