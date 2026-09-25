# ADR-0018: Menu bar label: letter, session and weekly percent, colored dots

- Status: accepted
- Date: 2026-09-25
- Source: user decision (feedback after the first install). Supersedes the menu bar label part of ADR-0011 and the menu bar use of emoji in ADR-0009.

## Context
The first label, `💼 45%  🏠 12%` with each percent colored, was too loud. It also showed only the session window.

## Decision
- **Mode `letter_percent`** (default) shows each profile as `{letter} {session %} {dot} - {weekly %} {dot}`, e.g. `P 2% ● - 63% ●   W 24% ● - 99% ●`.
  - `letter`: first letter of the profile name, uppercased (the id if the name is blank).
  - Only the dots carry the level color (ADR-0009 green/yellow/red; gray = no data). Letters and percents use the plain menu bar text color.
  - Font: 11 pt medium, monospaced digits (smaller than the system menu bar font).
  - No data: `?%` with a gray dot.
- `icon_only` is unchanged.
- **Config:** `display.menu_bar = letter_percent | icon_only`. `emoji_percent` is still accepted as a legacy name for `letter_percent`, so existing configs stay valid.
- **Rendering:** the label is an image (the menu bar strips colors from text). Its text color is picked from the menu bar's color scheme at render time.

## Consequences
- The profile emoji stays in the widgets, the dropdown cards and the statusline.
