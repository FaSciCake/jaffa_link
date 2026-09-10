# jaffa_link

QR-code file transfer for air-gapped PCs. `sender.py` runs on the air-gapped
machine, encodes a folder's contents as a looping QR slideshow; you film it
on your phone and send the video to a Telegram bot (`converter_bot.py`),
which decodes it back into files and returns a zip.

## Components

- **sender.py** — run on the air-gapped PC. Walks a folder, JSON-serializes
  `{path, base64 content}` per file, base45-encodes the whole blob (denser
  than base64 in QR alphanumeric mode), splits into chunks, renders each as
  a QR code, and full-screens a PySide6 slideshow (space to start/stop
  auto-play, arrow keys to step manually). `make_qr_image()` samples 4 of
  the library's 8 mask-pattern candidates instead of all 8 (~1.5-2x faster
  generation, negligible scan-reliability cost — `MASK_CANDIDATES` widens
  back to `range(8)` if ever needed). `run_slideshow()` auto-picks how many
  QR codes to show side by side per slide (`suggest_codes_per_row()`, lands
  on 2 for a 16:9 screen — a square code sized to screen height wastes the
  extra width otherwise); override with `--codes-per-row`.
  `--resend`'s chunk numbers are only meaningful relative to the exact
  folder contents + `--chunk-size` that produced them, but `build_chunks()`
  re-derives total/digest from scratch on every run with no memory of any
  earlier one — a file added/removed/edited (or a different `--chunk-size`)
  since the original send silently shifts every index's byte range, and a
  same-or-larger total that happens to still contain the requested index
  slips past a plain range check while still handing the receiver a wrong
  payload for it. A normal (non-`--resend`) run now writes its
  chunk-size/total/digest/per-file-sha256 manifest to `sender_state.json`
  (`save_send_state()`, gitignored — it's local run state, not source);
  `--resend` loads it and diffs the current folder against it
  (`diff_send_state()`) before doing anything else, refusing with a
  specific diagnosis (which file changed, or `--chunk-size`/folder path
  differs) if they don't match, or if no prior full send was recorded at
  all. This is what actually catches drift a plain out-of-range check
  can't — the followup guard against an index outside the freshly-rebuilt
  total is kept too, as a cheap fallback.
- **converter_bot.py** — Telegram bot (python-telegram-bot). Downloads each
  uploaded video or photo immediately, then hands it to a single
  background `video_worker()` task that processes queued jobs one at a
  time in receipt order — so several uploads (a transfer split across
  videos, or just sent in a burst) merge into the same transfer via the
  same accumulation logic used for `--resend` top-ups, instead of later
  ones being rejected while an earlier one is still scanning. A single
  *photo* of one QR frame is also accepted (`scan_image_sync()`, no frame
  loop — just one zxing-cpp pass) — convenient for topping up a chunk or
  two without filming a video of a slide that never changes; Telegram's
  photo compression is fine here since a corrupted read just fails its
  own per-chunk checksum and gets skipped rather than silently accepted.
  Scans frames with zxing-cpp/opencv, reassembles chunks by index,
  verifies the sha256 checksum, writes files, zips, sends back.
  `StatusMessage.update()`
  defaults `parse_mode="Markdown"` — every call site's text already used
  `*bold*`/`` `code` `` formatting, so a call that didn't pass parse_mode
  explicitly used to render the markup as literal characters (and one call
  site in the IncompleteTransfer path that *did* pass `parse_mode=` used to
  crash outright, since the method didn't accept the kwarg at all).
  `/reset` abandons an in-progress transfer; `/status` reports transfer +
  queue progress; `/help` (aliases `/start`) lists commands. If an
  upload's chunk total disagrees with the transfer already in progress,
  `process_video()` treats it as a different transfer and discards the
  old progress (it was never going to reassemble correctly against a
  different chunking anyway) — but says so explicitly via
  `IncompleteTransfer.reset_note`, prepended to the next status message,
  instead of silently vanishing the chunks the user already sent (which
  read as "combining broke" rather than what it actually was: a mismatch
  on the sender side, most commonly `sender.py --resend` run against
  drifted folder contents — see `sender_state.json` above).
  `scan_video_sync()` downscales frames wider/taller than `MAX_SCAN_DIM`
  (1600px) before handing them to zxing-cpp — barcode-detection cost scales
  with pixel count, and a QR code doesn't need 1080p/4K to decode reliably.
  Its progress bar shows *combined* transfer progress (baseline from earlier
  videos + new chunks this video found), not just this video's own count —
  otherwise a `--resend` video showing only late chunk indices displayed a
  misleadingly tiny percentage against the whole-transfer total. The
  IncompleteTransfer message and `/status` both render a `render_coverage_bar()`
  strip (one character per slice of the index range, shaded by how present
  that slice is) plus a `compress_ranges()` summary ("247-2624" instead of
  2378 separate numbers) — the `--resend` command argument itself stays a
  flat comma list since `sender.py --resend` doesn't parse range syntax, but
  it's capped at `MAX_RESEND_LIST_CHARS` (Telegram rejects any message over
  ~4096 chars — a large missing-chunk transfer blew right through that
  before the cap existed). The bar is `[bracket-wrapped]` (an unbounded run
  of "0%" characters with no border read as blank space, not "not started
  yet"), and a slice that's merely *mostly* present is never rounded up to
  the fully-present shade regardless of how small its gap is — otherwise a
  couple of missing chunks in an 80+-chunk slice vanish from the picture and
  a near-complete transfer looks indistinguishable from a finished one.
- **converter_config.py** — bot token + allowed Telegram user IDs.
  **Gitignored** (contains a live secret). Copy from
  `converter_config.example.py` and fill in real values.
- **test_pipeline.py** — end-to-end test of chunking/reassembly/checksum
  logic with synthetic frame data (no camera/video needed).
- **outgoing_message/** — drop files here before running `sender.py` (or the
  `send.*` launchers) with no folder argument. **Gitignored** (it's your
  payload, not source).
- **send.ps1** / **send.bat** — quick launchers for `sender.py`, using the
  `.venv-dev` interpreter directly (no manual activate needed). `send.bat`
  is double-click-friendly; both forward all CLI args, e.g.
  `.\send.bat --chunk-size 2500` or `.\send.bat --resend 5,12,47`. With no
  folder argument, `send.ps1` defaults to `outgoing_message` (creating it if
  needed) rather than falling through to sender.py's own default of the
  current directory — running bare from this project's own root would
  otherwise walk the whole repo, `converter_config.py`'s live bot token
  included, straight into the QR slideshow.

## Wire format

Each QR frame is a plain string, one of:
- `HASH:<sha256 hex>` — checksum of the full base45 payload, sent once.
- `<index>/<total>/<checksum>:<base45 chunk>` — one piece of the payload,
  1-indexed. `<checksum>` is the first 8 hex chars of `sha256(payload)`,
  checked by `scan_video_sync()` before a decoded chunk is ever stored.
  zxing-cpp can report a frame as "valid" even when it's been corrupted past
  what the QR's own error correction (`ERROR_CORRECT_L`) can recover —
  motion blur or video-compression artifacts on a fast-changing slideshow
  are enough. Without this, such a frame silently poisoned that chunk index
  for the rest of the scan (first successful-looking decode wins, per
  index) and the corruption only surfaced as a whole-payload hash mismatch
  at the very end, with no way to tell which chunk was bad or let a later,
  cleaner loop-pass of the same slideshow (or a follow-up video) correct
  it. A frame that fails its own checksum is now just discarded — treated
  as not-yet-seen — so a subsequent good read of the same index, in this
  video or a later one, still gets accepted normally.

The receiver de-dupes by index and can complete a transfer from multiple
partial videos as long as the reported `total` matches across them. If the
final whole-payload checksum still doesn't match after every chunk has
individually passed its own checksum, `process_video()` clears the
accumulated transfer state (`reset_progress()`) before raising — otherwise
a resend's baseline started out already "100% collected" (just wrong),
which made `/status` and the scan progress bar read as permanently stuck
instead of the retry actually restarting from zero.

## Environment

Single shared venv at `.venv-dev` (PySide6, qrcode, base45, opencv-python,
zxing-cpp, python-telegram-bot) covers both scripts. In production the
sender side would normally only need PySide6/qrcode/base45 — the venv here
is a dev convenience since both scripts and test_pipeline.py live in this
repo together.

## Running

```
.\send.bat                       # sender, scans .\outgoing_message
.\.venv-dev\Scripts\python.exe converter_bot.py   # bot, run on your own machine
.\.venv-dev\Scripts\python.exe test_pipeline.py   # tests
```
