# my-term

One Python module, `my_term.py`, that knows the terminal: **which one it is**, **an image pasted into it**, and **an
image shown in it** -- for iTerm2 on macOS and Secure ShellFish on iOS and iPadOS, directly or inside tmux, over ssh
or not. A tool asks it by capability and never looks at a terminal itself, so a new terminal changes this file alone.

Standard library only, Python 3.9 or newer, macOS or Linux (it uses `termios`).

## Use

Put `my_term.py` beside your tool (a copy or a link) and import it:

```python
import my_term

term = my_term.detect()          # once per run: up to 200 ms
print(term.said())               # terminal: iterm2 (XTVERSION 'iTerm2 3.7.7') -- images drawn

got = my_term.image_from(typed, keep_dir)   # a typed or pasted answer: an image, or None (text)
if got and got.how == "elsewhere":
    print(got.path, "--", got.said)          # a path on another machine: what to do instead

if not my_term.show(term, png, cols=40, rows=10):
    print(png_path)              # cannot be drawn here: name the file
```

- **`detect()` -> `Terminal`** (`kind`: `iterm2`, `shellfish` or `other`; `answer`: what named it; `tmux`,
  `passthrough`, `at_terminal`; `draws()`; `said()` one line for a `-V`). Directly, the terminal is asked
  XTVERSION (`ESC [ > q`): the answer comes over ssh, where `TERM_PROGRAM` and `LC_TERMINAL` do not. DA1 is sent
  after it, so a terminal that does not know XTVERSION ends the wait at once. Inside tmux, tmux answers XTVERSION
  for itself, so tmux is asked instead: the terminal its client names (`client_termtype`) and whether
  `allow-passthrough` is on. Not a terminal, or no answer within 200 ms: `other`, and no image is drawn.
- **`image_from(text, keep_dir)` -> `Pasted | None`**: a path naming a readable image file here is that file
  (ShellFish uploads a pasted image and types its path; a file dragged in); an absolute image path that is not here
  is `elsewhere` (iTerm2's "Save to Temp File and Paste Path" over ssh types a path on the Mac), with the hint to
  paste it as base64; base64 that decodes to a PNG, JPEG, GIF or WebP (iTerm2's "Paste Base64-Encoded Contents",
  line breaks and all) is written into `keep_dir` as `pasted-<n>.<type>`, written whole before it gets its name and
  never over another. Words that happen to be valid base64 (`done`) decode to no image: text.
- **`show(term, png, cols, rows)` -> `bool`**: the PNG drawn at the cursor by kitty's graphics protocol, `cols`
  cells by `rows`, the cursor left after it on its last row as text of that size would leave it. Inside tmux the
  image goes through passthrough, placed by the module itself (the terminal would draw it at its own cursor, the
  window's top-left): it keeps `rows` lines free in the pane, asks the pane for its cursor (DSR) and tmux for the
  pane's place in the window. tmux does not know the image: it is gone when tmux redraws those cells. `False`: it
  cannot be drawn here (`term.draws()`, or the pane's place not found) and nothing was written -- name the file.

**What is drawn where** -- `Terminal.draws()`:

| terminal | directly | inside tmux, `allow-passthrough on` |
|---|---|---|
| iTerm2 | yes | yes (seen: iTerm2 3.7.7, tmux 3.8) |
| ShellFish | yes | not yet: its own page says images do not come through tmux, and no run has shown otherwise |
| anything else | no | no |

One protocol, kitty's: both terminals draw it, and only it passes tmux. iTerm2's own (OSC 1337) is not used.

## From a shell

```
usage: my_term.py [-h] [--version] [--run-tests [<filter>]] [kind | show <PNG>]
```

- `python3 my_term.py kind` -- the terminal, what named it, whether images are drawn.
- `python3 my_term.py show <PNG>` -- draws it 20 cells wide, or says why not.
- `python3 my_term.py --run-tests [<filter>]` -- its tests, TAP (`1..N`, then `ok K - title` per test), those whose
  title holds the filter. A terminal is faked on a pty, tmux by a stand-in: no real terminal is needed.
- `python3 my_term.py --version` -- the version, git's describe when run in its own checkout, and the build (the
  file's own sha-256, 12 digits): two copies that differ never print the same line.

Errors are one line on stderr, `ERROR <code> [<rc>]: ...`; Ctrl-C ends with one line and 130.

## Learned

### 2026-11-23 13:25  #FROM:`man tmux` (tmux 3.8)

- `client_termtype`: "Terminal type of client, if available"; `allow-passthrough [on | off | all]`. That
  `client_termtype` holds the client terminal's XTVERSION answer is my reading, not observed (a client is needed).

### 2026-11-23 13:20  #FROM:tmux 3.8 (Homebrew, macOS), observed: `my_term.ask` and `my_term.show` in a detached pane

- tmux answers XTVERSION itself, `ESC P >|tmux 3.8 ESC \`, and DA1 `ESC [?1;2;4c` -- a tool inside tmux asking the
  terminal learns only "tmux".
- `display-message -p` expands an option by name as a format: `#{allow-passthrough}` gave `off`, then `on` after
  `set -g allow-passthrough on`; `#{status}` `on`, `#{status-position}` `bottom`, `#{pane_top}` `0`.
- `#{client_termtype}` is empty with no client attached.
- `show()` inside it: the pane answered DSR, `pane_top` and the rest came, it returned True and the pane kept the
  rows free. Whether the image then lands at the right cell on a real terminal is not observed here (no client).

### 2026-11-22 15:41  #FROM:a probe run by the author: iTerm2 3.7.7 on a Mac, over ssh to another Mac, tmux 3.8

- The terminal names itself to XTVERSION (`ESC P >|iTerm2 3.7.7 ESC \`) although the ssh session carries no
  `TERM_PROGRAM` or `LC_TERMINAL`; DA2 answers `>64;2500;0`.
- Over plain ssh both an iTerm2 inline image (OSC 1337) and a kitty-protocol image (`ESC _G f=100,a=T,c=4,r=2`)
  are drawn; without `c`/`r` the kitty image is drawn at its pixel size.
- Inside tmux 3.8 with `allow-passthrough on` the kitty image comes through, drawn at the window's top-left, not at
  the pane's cursor; the iTerm2 one does not.
- A 160 KB screenshot pasted with "Paste Base64-Encoded Contents": ONE bracketed paste of 215,682 bytes, base64
  broken into lines with CR, arriving in 280 reads over 10 s.

### 2026-11-22 14:20  #FROM:https://shellfishapp.com/ and its App Store page https://apps.apple.com/app/apple-store/id1336634154

- Secure ShellFish 2026.35. Pasting an image or a file uploads it to the server; 2026.16 added inserting the
  uploaded file's path, 2026.23 names like `pasted-from-shellfish-20260701-143012.png`. The upload folder is not
  stated.
- Inline images by kitty's graphics protocol (2026.35); "Images do not come through tmux or Mosh". The iTerm2
  protocol and sixel: not stated.

### 2026-11-22 07:33  #FROM:https://iterm2.com/documentation-images.html

- iTerm2 draws images by its own protocol (OSC 1337) and also supports kitty's graphics protocol.

### 2026-11-19 23:23  #FROM:a probe run by the author: iTerm2 3.7.7 on macOS, Cmd+V with a PNG in the clipboard

- Cmd+V with an image opens iTerm2's "Paste Image" dialog: "Save to Temp File and Paste Path", "Paste
  Base64-Encoded Contents" or Cancel. The path arrives as one bracketed paste,
  `/var/folders/.../pasted-image-<time>-<uuid>.png`; base64 as one bracketed paste starting `iVBORw0KGgo`.
- Ctrl+V reaches the program as one byte, `0x16`, no paste.

What follows for this module: XTVERSION directly, tmux asked inside tmux; kitty's protocol only, placed by the
module through passthrough; a pasted image is a file whichever way it came. ShellFish through tmux stays
"not drawn" until a run shows it drawing.

## License

GPL-3.0 -- see `LICENSE`.
