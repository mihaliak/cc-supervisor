"""Terminal modes the child turned on, so suspend and exit hand back a sane terminal (ADR-0022).

A pure tracker over the child's output. It follows:
- DEC private modes: mouse (1000, 1002, 1003, 1006), focus reports (1004), bracketed paste
  (2004), alternate screen (1049) and cursor visibility (25);
- the kitty keyboard stack (`CSI > f u` push, `CSI < n u` pop, `CSI = f ; m u` set), kept per
  screen like the terminal does;
- xterm modifyOtherKeys (`CSI > 4 ; n m`).

`reset()` gives the bytes that put the terminal back to its defaults (Ctrl-Z, child exit or
crash), `replay()` the bytes that turn the child's modes on again (after `fg`). Sequences split
across reads are carried over.
"""

from __future__ import annotations

import re

# tracked DEC private modes → the terminal's default (True = set)
DEC_DEFAULTS = {
    1000: False,
    1002: False,
    1003: False,
    1006: False,
    1004: False,
    2004: False,
    1049: False,
    25: True,
}
ALT_SCREEN = 1049
# a prefixed CSI sequence we may care about, or RIS (full reset)
_SEQ = re.compile(rb"\x1b\[([?<>=])([0-9;:]*)([hlmu])|\x1bc")
# an unfinished sequence at the end of a read
_PARTIAL = re.compile(rb"\x1b(?:\[[?<>=]?[0-9;:]*)?\Z")
_TAIL_MAX = 64


def _ints(params: bytes) -> list[int]:
    """`1;2:3` → `[1, 2]` (sub-parameters dropped, empty → 0)."""
    return [int(p.split(b":")[0] or b"0") for p in params.split(b";")] if params else []


def _dec(mode: int, on: bool) -> bytes:
    return b"\x1b[?%d%s" % (mode, b"h" if on else b"l")


class TermModes:
    """Feed it the child's output; ask it how to reset or replay the terminal."""

    def __init__(self) -> None:
        self.dec: dict[int, bool] = {}
        # per screen (False = main, True = alternate): [base flags, pushed flags…]
        self.kitty: dict[bool, list[int]] = {False: [0], True: [0]}
        self.other_keys = 0
        self._tail = b""

    def feed(self, data: bytes) -> None:
        if self._tail:
            data = self._tail + data
            self._tail = b""
        if b"\x1b" not in data:
            return
        for match in _SEQ.finditer(data):
            self._apply(match)
        start = data.rfind(b"\x1b", max(0, len(data) - _TAIL_MAX))
        if start >= 0 and _PARTIAL.match(data, start):
            self._tail = data[start:]

    def _apply(self, match: re.Match[bytes]) -> None:
        prefix, params, final = match.groups()
        if prefix is None:  # RIS: the terminal is back to its defaults
            self.dec.clear()
            self.kitty = {False: [0], True: [0]}
            self.other_keys = 0
            return
        nums = _ints(params)
        if prefix == b"?" and final in (b"h", b"l"):
            for mode in nums:
                if mode in DEC_DEFAULTS:
                    self.dec[mode] = final == b"h"
        elif final == b"u" and prefix != b"?":
            self._kitty(prefix, nums)
        elif prefix == b">" and final == b"m" and nums[:1] == [4]:
            self.other_keys = nums[1] if len(nums) > 1 else 0

    def _kitty(self, prefix: bytes, nums: list[int]) -> None:
        stack = self.kitty[self.dec.get(ALT_SCREEN, False)]
        first = nums[0] if nums else 0
        if prefix == b">":
            stack.append(first)
        elif prefix == b"<":
            count = first or 1
            if count >= len(stack):  # popping everything resets the flags
                stack[:] = [0]
            else:
                del stack[-count:]
        else:  # `=`: 1 set, 2 add bits, 3 remove bits
            how = nums[1] if len(nums) > 1 else 1
            if how == 1:
                stack[-1] = first
            elif how == 2:
                stack[-1] |= first
            elif how == 3:
                stack[-1] &= ~first

    def _changed(self) -> list[tuple[int, bool]]:
        return [
            (mode, on)
            for mode, on in self.dec.items()
            if mode != ALT_SCREEN and on != DEC_DEFAULTS[mode]
        ]

    def _kitty_reset(self, alt: bool) -> bytes:
        stack = self.kitty[alt]
        out = b"\x1b[<%du" % (len(stack) - 1) if len(stack) > 1 else b""
        return out + (b"\x1b[=0;1u" if stack[0] else b"")

    def _kitty_replay(self, alt: bool) -> bytes:
        base, *pushed = self.kitty[alt]
        out = b"\x1b[=%d;1u" % base if base else b""
        return out + b"".join(b"\x1b[>%du" % flags for flags in pushed)

    def reset(self) -> bytes:
        """Bytes that undo every mode the child changed (empty when nothing is on)."""
        alt = self.dec.get(ALT_SCREEN, False)
        out = [self._kitty_reset(alt)]
        out += [_dec(mode, DEC_DEFAULTS[mode]) for mode, _on in self._changed()]
        if alt:  # the main screen's kitty stack is only reachable after leaving
            out += [_dec(ALT_SCREEN, False), self._kitty_reset(False)]
        if self.other_keys:
            out.append(b"\x1b[>4m")
        return b"".join(out)

    def replay(self) -> bytes:
        """Bytes that turn the child's modes on again after a `reset()`."""
        out = [self._kitty_replay(False)]
        if self.dec.get(ALT_SCREEN, False):
            out += [_dec(ALT_SCREEN, True), self._kitty_replay(True)]
        out += [_dec(mode, on) for mode, on in self._changed()]
        if self.other_keys:
            out.append(b"\x1b[>4;%dm" % self.other_keys)
        return b"".join(out)
