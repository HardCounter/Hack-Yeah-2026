# Visuals

Status: design direction, not implemented. Layout and panels are defined in [dashboard-ui.md](dashboard-ui.md);
this file defines how they look.

## Visual identity: audit ledger

Domain is banking compliance (KYC, AML), so the UI looks like a printed audit report brought to life.
Think annual report of a 150-year-old private bank, not a startup SaaS.

- Off-white paper background, black ink, thin hairline rules, no cards with shadows.
- Serif for headings and prose; monospace for numbers, IDs, latencies and payloads.
- Color carries meaning only: **red = BLOCK**, **amber = REDACT / ESCALATE**, **muted green = ALLOW**. Nothing else is colored.
- Verdicts are shown as **rubber stamps**: `BLOCKED`, `REDACTED`, `ESCALATED TO HUMAN`, `ALLOWED`.
- Light theme only (bright arena, varied laptop screens).
- Light-academia palette: cream paper, warm ink, oxblood, ochre, forest green.

### What "old money" means in practice
Old money is **restraint**. Nothing shouts, nothing moves without a reason, nothing is decorated for its own sake.

| Do | Don't |
|---|---|
| Lots of white space and wide margins | Filling every pixel with widgets |
| One big, calm headline figure per section | Rows of KPI tiles |
| Typography carries the hierarchy (size, small caps, weight) | Boxes, badges and icons carry the hierarchy |
| Hairline rules and double rules, like a ledger | Cards, shadows, rounded pills |
| Formal, short copy: "Request declined." | Casual copy: "Oops! 🚫 Nice try!" |
| One accent moment per screen (the stamp) | Accent color everywhere |

### Not Goldman branding
We are building **our** product for their challenge. Don't use the Goldman Sachs logo, name as a wordmark,
their typeface or their exact blue. "Classy private-bank feel" yes, imitation no.

---

## Color tokens

Contrast ratios measured on `--paper` (WCAG AA for body text needs ≥ 4.5).

| Token | Hex | Use | Contrast |
|---|---|---|---|
| `--paper` | `#F5F0E6` | Page background (warm cream) | — |
| `--paper-2` | `#ECE5D6` | Inset areas: textarea, code, selected row | — |
| `--rule` | `#D6CCB8` | Hairline rules and borders (non-text only) | 1.4 |
| `--ink` | `#1F1D1A` | Text, primary buttons, strong rules | 14.8 |
| `--ink-2` | `#57514A` | Secondary text, labels, units | 6.9 |
| `--ink-3` | `#7A7366` | Large or decorative text only (≥ 18 px), placeholders | 4.1 ✗ small text |
| `--block` | `#8C2323` | Oxblood. BLOCK, failed test | 7.8 |
| `--warn` | `#8A5A12` | Ochre. REDACT, ESCALATE, APPROVE, ALERT | 5.2 |
| `--allow` | `#2E5A3A` | Forest green. ALLOW, passed test | 7.0 |
| `--brass` | `#8C6D32` | **Only** the wordmark's seal and the double rule under the letterhead | 4.3 ✗ small text |

Rules:
- Verdict colors are always paired with the **word** (`BLOCKED`) and never used alone. Oxblood and forest
  green are hard to tell apart for red-green colorblind users.
- No gradients, no transparency effects, no pure black (`#000`) or pure white (`#FFF`) anywhere.
- Brass is the single exception to "nothing else is colored", and only as decoration. If it starts
  spreading to buttons or links, remove it.

---

## Typography

All fonts are self-hosted `woff2` files in `/static/fonts` (no CDN; bad Wi-Fi). All are SIL OFL licensed.

| Role | Font | Settings |
|---|---|---|
| Headings, prose, big figures | **Source Serif 4** (variable, optical sizes) | Headings 400–600 weight. Big figures use lining numerals |
| Labels, section titles, buttons | Source Serif 4, **small caps** | `font-variant-caps: all-small-caps; letter-spacing: .08em` |
| Numbers, IDs, latencies, payloads, timestamps | **IBM Plex Mono** | `font-variant-numeric: tabular-nums` so columns line up |

Why serif instead of a plain sans: every AI dashboard uses Inter. A well-set serif with small caps
is the fastest way to look like a private bank and not a startup.

Scale (rem, base 16 px): `0.8125` captions · `0.9375` body/table · `1.125` lead · `1.5` section title ·
`2.25` page title · `3.5` headline figure. Line height 1.5 for prose, 1.25 for headings.

Section numbering uses roman numerals, like a formal report: **I. Try to break it**, **II. Agent said done. Was it?**, **III. Ledger**.

---

## Layout

- Max content width **1280 px**, centered; outer margin 48 px on laptops, 16 px on phones.
- 12-column grid, 32 px gutters. The two main panels are 6 + 6, separated by a **vertical hairline**, not a gap with cards.
- Vertical rhythm on an 8 px grid. Sections are separated by 48–64 px of space plus one hairline.
- Must fit a 14" laptop (1366 × 768 effective) with no horizontal scroll. Below 900 px the panels stack;
  on phones (QR visitors) only the attack console and live feed are shown expanded, the rest collapse.

### Letterhead (header)
```
 ◆ AI CONTROL LAYER                         POLICY v12 · STRICT ▾     ● LIVE     [QR]
═══════════════════════════════════════════════════════════════════════════════════  ← brass double rule
```
- Wordmark in serif small caps with a small brass lozenge (◆) as the seal. No logo image.
- Policy version and mode in mono. The live indicator is an **ink** dot (filled = live, hollow = offline),
  not green, because green means ALLOW.

---

## Components

### Verdict stamp
The signature element. One stamp per result, never more than one visible per panel.

- Serif all-small-caps, letter-spacing .12em, 1.25 rem, in the verdict color.
- Border: 2 px solid + an inner 1 px line (`outline: 1px solid; outline-offset: -5px`), 2 px radius.
- Rotated `-3deg`. `mix-blend-mode: multiply` so it looks inked onto the paper.
- When a verdict arrives: scale 1.12 → 1 and opacity 0 → 1 over 140 ms `ease-out` (a soft "thump"). Nothing else on the page bounces.
- Text: `BLOCKED` · `REDACTED` · `ESCALATED TO HUMAN` · `ALLOWED`. Below it, one line of plain explanation
  in `--ink-2`, e.g. *Instruction override detected in attached document (score 0.94).*

### Ledger table (pipeline trace, live feed, test results)
- No zebra stripes, no cell borders. Hairline `--rule` between rows; a 1 px `--ink` rule under the header.
- Header in small caps `--ink-2`. Numbers right-aligned, mono, tabular. Units (`ms`, `tok`) in `--ink-2`.
- Row numbers `01 02 03` in mono `--ink-2`, like a journal.
- New rows (live feed) appear at the top with a 600 ms `--paper-2` background that fades out. No sliding.
- Timestamps: `14:02:11` in the table; full `3 Oct 2026, 14:02:11 CEST` on hover.

### Headline figure
Replaces KPI tiles. One per section, set like an annual report:

```
13 of 14                      0 false positives
attacks caught today          on 40 legitimate requests
```
Big serif lining numerals (3.5 rem), caption in small caps underneath. Never shown for data that isn't live; show *Not yet run.* in italic instead.

### Test suite strip
- One 10 × 10 px square per test case, 2 px gap, grouped by control family with a small-caps label above each group.
- Pass = `--allow` fill, fail = `--block` fill, not run = hairline outline only.
- Hover/tap opens a footnote-style popover: case ID, expected vs actual, in mono.

### Buttons and inputs
- Primary: `--ink` background, `--paper` text, small caps, 2 px radius, 40 px tall. Hover: `--ink-2` background.
- Secondary: transparent with 1 px `--ink` border. Tertiary: text link with underline.
- Textarea: `--paper-2` background, 1 px `--rule` border, mono text. Focus: 2 px `--ink` outline, offset 2 px.
- Presets ("Try this:") are a ruled list, like an index: `→ Injection hidden in a document`. Not chips or pills.
- No icons, except typographic glyphs where they add meaning (`→ ▾ ●`).

### Charts (only where they earn it)
- Budget: one thin horizontal bar with tick marks at 50/80/100 %, like a printed gauge. Ink fill; turns `--warn` above the alert threshold and `--block` at the limit.
- Latency: optional small line chart in ink, hairline axes, no gridlines, direct labels instead of a legend.
- No donuts, no maps, no sparklines in tiles (banned list).

---

## Motion
- Durations 120–200 ms, `ease-out`. Pipeline rows reveal one after another with a 60 ms stagger, so
  the judge reads the controls in order.
- Only the stamp has a distinct animation. Everything respects `prefers-reduced-motion` (no motion, instant state).

## Copy tone
Formal, short, specific, like a compliance officer writing a memo.
- ✓ *Request declined. A PESEL number and an IBAN were removed before the model saw the prompt.*
- ✗ *Whoa! We caught some sensitive data 🙈*
- Use full words (*Escalated to human reviewer*), sentence case for prose, small caps for labels.

## Accessibility (non-negotiable)
- Body text contrast ≥ 4.5 (see table). `--ink-3` and `--brass` never for small text.
- Verdicts always have a text label; color is never the only signal.
- Visible keyboard focus on everything; Ctrl+Enter submits; the stamp is announced via `aria-live="polite"`.
- All payloads rendered as plain text (`textContent`), never HTML.

---

## CSS starting point

```css
:root {
  --paper: #F5F0E6; --paper-2: #ECE5D6; --rule: #D6CCB8;
  --ink: #1F1D1A; --ink-2: #57514A; --ink-3: #7A7366;
  --block: #8C2323; --warn: #8A5A12; --allow: #2E5A3A; --brass: #8C6D32;
  --serif: "Source Serif 4", Georgia, serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}
body { background: var(--paper); color: var(--ink); font: 400 0.9375rem/1.5 var(--serif); }
.label { font-variant-caps: all-small-caps; letter-spacing: .08em; color: var(--ink-2); }
.num, code, .id { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.letterhead { border-bottom: 3px double var(--brass); }
.stamp {
  display: inline-block; padding: .4em .9em; transform: rotate(-3deg);
  font-variant-caps: all-small-caps; letter-spacing: .12em; font-size: 1.25rem;
  border: 2px solid currentColor; outline: 1px solid currentColor; outline-offset: -5px;
  border-radius: 2px; mix-blend-mode: multiply;
}
.stamp.block { color: var(--block); } .stamp.warn { color: var(--warn); } .stamp.allow { color: var(--allow); }
@media (prefers-reduced-motion: no-preference) {
  .stamp.in { animation: thump 140ms ease-out; }
  @keyframes thump { from { opacity: 0; transform: rotate(-3deg) scale(1.12); } }
}
```

---

## Absolutely banned (generic AI dashboard look)
Dark navy with purple/blue gradients, glassmorphism, rows of KPI tiles with sparklines, emoji icons,
donut charts of fake traffic, world maps, a chatbot bubble.
Also: Inter as the main font, rounded pill badges, drop shadows, pure black or white, more than one stamp per panel.

---

## Open questions
1. Product name for the letterhead (it sets the tone more than any color).
2. Keep the brass accent, or go strictly ink + verdict colors?
3. Does the brainstorm's **APPROVE** verdict (human approval needed) share ochre with ESCALATE, or get its own stamp text only?
