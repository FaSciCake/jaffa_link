"""
converter_bot.py — QR Video Converter Bot
Receives a .mov/.mp4 video of the QR slideshow, decodes it,
zips the result and sends it back.

If a video is missing some chunks, progress is kept: send another
(shorter) video from `sender.py --resend <missing indices>` and it'll
merge with what's already been collected instead of starting over.
Send /reset to abandon an in-progress transfer and start fresh.

Videos are queued, not rejected: send several in a row (or while one is
still processing) and they're handled one at a time, in order, each
merging its chunks into the same transfer via the same accumulation
logic used for --resend top-ups. /status reports queue + progress.

Works the same whether the sender shows one QR code at a time or several
side by side on the same slide — zxing-cpp already detects and decodes
every barcode it finds in a frame, so this file doesn't need to know how
many codes are on screen at once.

Requirements:
    pip install python-telegram-bot opencv-python zxing-cpp base45

Usage:
    python converter_bot.py
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import base45
import cv2
import zxingcpp
from telegram import Update, Message
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from telegram.constants import ChatAction

from converter_config import BOT_TOKEN, ALLOWED_USER_IDS

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent
RECEIVED_DIR = BASE_DIR / "received"
ARCHIVE_DIR  = BASE_DIR / "archived"
TEMP_DIR     = BASE_DIR / "temp_videos"

for d in (RECEIVED_DIR, ARCHIVE_DIR, TEMP_DIR):
    d.mkdir(exist_ok=True)

# ── State — one job at a time, chunks accumulate across uploads ────────────────

CHUNK_RE = re.compile(r'^(\d+)/(\d+)/([0-9a-f]{8}):(.+)$')
HASH_RE  = re.compile(r'^HASH:([0-9a-f]{64})$')

VIDEO_EXTENSIONS = (".mov", ".mp4", ".avi", ".mkv")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")


def _chunk_checksum_ok(idx: int, chunk_cs: str, payload: str, source: str) -> bool:
    """Shared per-chunk checksum check for scan_video_sync/scan_image_sync.
    Logs a diagnostic on mismatch -- payload length + head/tail -- since a
    discarded chunk otherwise leaves no trace of *why* it was rejected,
    which matters for telling a genuine misread apart from an encode/decode
    bug (stray whitespace, a case/charset mismatch, truncation, etc.)."""
    expected = hashlib.sha256(payload.encode('ascii')).hexdigest()[:8]
    if expected == chunk_cs:
        return True
    logger.warning(
        "Chunk %d checksum mismatch (%s): QR carried checksum=%s, payload "
        "(len=%d) actually hashes to %s. payload head=%r tail=%r",
        idx, source, chunk_cs, len(payload), expected, payload[:24], payload[-24:],
    )
    return False

# Minimum real time between progress-bar edits. Scanning a pre-recorded
# video isn't real-time-bound (cv2 decodes frames as fast as the CPU
# allows), so gating purely on chunk count (the old "every 5 chunks" rule)
# could fire edits many times a second on a fast decode -- unnecessary API
# load regardless of exactly where Telegram's own throttling kicks in.
# Gating on wall-clock time keeps it responsive without that, independent
# of how fast any given video happens to decode.
PROGRESS_EDIT_INTERVAL = 0.8  # seconds

# Telegram rejects any message text over ~4096 characters outright. A
# badly-scanned video can leave hundreds or thousands of chunks missing --
# see MAX_RESEND_LIST_CHARS usage in the IncompleteTransfer handler below.
MAX_RESEND_LIST_CHARS = 3500

# Downscale frames wider/taller than this before handing them to zxing-cpp.
# Barcode detection cost scales with pixel count, but a QR code doesn't need
# 1080p/4K resolution to decode reliably -- it needs each module to be a few
# pixels wide, which 1600px on the long side comfortably covers even for two
# codes side by side. Cuts scan time substantially on typical phone video.
MAX_SCAN_DIM = 1600

is_busy = False  # True while the worker is actively processing a job (distinct from queue backlog)

# Videos are downloaded as soon as they arrive but *processed* one at a
# time by video_worker(), in receipt order. This is what lets multiple
# uploads (a transfer split across videos, or just sent back-to-back)
# merge into one transfer automatically instead of the later ones being
# rejected while the first is still scanning.
pending_videos: asyncio.Queue["VideoJob"] = asyncio.Queue()


@dataclass
class VideoJob:
    video_path: Path
    fname:      str
    status:     "StatusMessage"
    chat_id:    int
    kind:       str = "video"  # "video" or "image" -- picks scan_video_sync vs scan_image_sync


def escape_backticks(text: str) -> str:
    """Defang literal backticks in untrusted text (e.g. a phone's filename)
    so it can't break out of a Markdown code span and crash the send."""
    return text.replace('`', "'")


COVERAGE_WIDTH  = 30
COVERAGE_SHADES = "·░▒▓█"  # 5 levels: 0%, ~25%, ~50%, ~75%, 100% present


def render_coverage_bar(present: set[int], total: int, width: int = COVERAGE_WIDTH) -> str:
    """Compact visual map of which part of the 1..total chunk range is
    present. Each character is one equal-sized slice of the index range,
    shaded by how much of that slice has actually been collected -- so a
    missing tail, a missing head, or scattered gaps are all visible at a
    glance instead of buried in a list of numbers.

    Wrapped in [brackets] so the bar's full extent is visible even when
    most of it is still empty -- a run of bare "0%" characters with no
    border was easy to mistake for blank space rather than "not started
    yet". A slice that's merely *mostly* present is also never rounded up
    to the fully-present shade, however small its gap -- otherwise a
    near-complete transfer (e.g. missing 2 chunks out of an 87-chunk
    slice) renders identically to a complete one and the handful of
    missing chunks vanish from the picture entirely.
    """
    if total <= 0:
        return "[]"
    max_level = len(COVERAGE_SHADES) - 1
    out = []
    for w in range(width):
        lo = int(w * total / width) + 1
        hi = max(lo, int((w + 1) * total / width))
        span = hi - lo + 1
        have = sum(1 for i in range(lo, hi + 1) if i in present)
        if have == span:
            level = max_level
        elif have == 0:
            level = 0
        else:
            level = max(1, min(max_level - 1, round(have / span * max_level)))
        out.append(COVERAGE_SHADES[level])
    return f"[{''.join(out)}]"


def compress_ranges(sorted_indices: list[int]) -> list[str]:
    """[5,6,7,9,12,13] -> ['5-7', '9', '12-13'] -- turns a flat list of
    missing chunk numbers into human-readable spans."""
    if not sorted_indices:
        return []
    spans = []
    start = prev = sorted_indices[0]
    for i in sorted_indices[1:]:
        if i == prev + 1:
            prev = i
            continue
        spans.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = i
    spans.append(str(start) if start == prev else f"{start}-{prev}")
    return spans


# Progress persists across multiple video uploads so a short "topped up"
# video (sender.py --resend ...) can complete a transfer that an earlier
# video didn't fully capture, instead of having to redo everything.
accumulated_chunks: dict[int, str] = {}
accumulated_total:  int | None     = None
accumulated_hash:   str | None     = None


def reset_progress():
    global accumulated_chunks, accumulated_total, accumulated_hash
    accumulated_chunks = {}
    accumulated_total   = None
    accumulated_hash    = None


class IncompleteTransfer(Exception):
    """Raised when, after merging this video's contribution, some chunks
    are still missing. Not a hard failure — progress is kept."""
    def __init__(self, missing: list[int], found: int, total: int, reset_note: str | None = None):
        self.missing     = missing
        self.found       = found
        self.total       = total
        # Set when this upload's chunk total didn't match the in-progress
        # transfer's, so process_video wiped prior progress and started
        # fresh from just this upload -- surfaced to the user rather than
        # silently discarding whatever they'd already sent (see
        # process_video's total-mismatch check).
        self.reset_note  = reset_note
        super().__init__(f"{found}/{total} chunks collected, {len(missing)} still missing")


# ── Auth ───────────────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


# ── Progress message helper ────────────────────────────────────────────────────

class StatusMessage:
    """Wraps a Telegram message and lets us edit it in-place for live updates."""

    def __init__(self, message: Message):
        self._msg    = message
        self._text   = message.text or ""
        self._last   = self._text

    async def update(self, text: str, force: bool = False, parse_mode: str | None = "Markdown"):
        """Edit the message only if text actually changed (avoids flood limits).

        Defaults to Markdown parsing since every call site formats its text
        with *bold* / `code` markup — without passing parse_mode through,
        Telegram was rendering it as literal asterisks and backticks instead
        of actually applying it.
        """
        if not text.strip():
            # Telegram rejects an edit with "Bad Request: message text is
            # empty" -- guard against ever sending one instead of finding
            # out from a failed HTTP call.
            logger.warning("StatusMessage.update() called with blank text -- skipping edit.")
            return
        if text == self._last and not force:
            return
        try:
            self._msg = await self._msg.edit_text(text, parse_mode=parse_mode)
            self._last = text
        except TelegramError as e:
            # Was silently swallowed before -- log it so a failed edit (e.g.
            # this exact "message too old" or "text is empty" case) actually
            # shows up somewhere instead of just quietly not updating.
            logger.warning("StatusMessage edit failed (%s) for text: %r", e, text[:300])


# ── Core processing ────────────────────────────────────────────────────────────

async def process_video(video_path: Path, status: StatusMessage, kind: str = "video") -> tuple[Path, int]:
    """
    Full pipeline for one uploaded video or image: scan → merge into the
    running transfer → (if complete) verify checksum → reassemble → write →
    zip.

    `kind` picks the scanner: "video" walks frames with scan_video_sync
    (the original camera-recording path); "image" hands a single still
    photo to scan_image_sync -- handy for topping up just one or two
    missing chunks via `--resend`, where filming a video of a code that
    never even changes slides is more fiddly than just snapping a photo.

    Raises IncompleteTransfer if chunks are still missing after merging
    this upload's contribution (progress is kept for the next one).
    """
    global accumulated_chunks, accumulated_total, accumulated_hash
    loop = asyncio.get_event_loop()

    # Snapshot rather than handing the live dict into the executor thread --
    # the scanners only read it (for combined-progress display), and a
    # snapshot avoids any doubt about touching shared state cross-thread.
    baseline = set(accumulated_chunks)
    continuing_note = f" (continuing from {len(baseline)}/{accumulated_total})" if baseline else ""

    # Step 1 — Scan
    if kind == "image":
        await status.update(f"🖼 *Step 1/4* — Scanning photo…{continuing_note}")
        video_chunks, video_total, video_hash = await loop.run_in_executor(
            None, scan_image_sync, video_path, baseline
        )
    else:
        await status.update(f"🎬 *Step 1/4* — Scanning video frames…{continuing_note}")
        video_chunks, video_total, video_hash = await loop.run_in_executor(
            None, scan_video_sync, video_path, status, loop, baseline
        )

    if not video_chunks or video_total is None:
        what = "photo" if kind == "image" else "video"
        raise ValueError(f"No QR chunks found in the {what}. Is this the right file?")

    # If this upload reports a different total than what's accumulated so
    # far, treat it as a new/different transfer rather than mixing the two
    # -- most often because the sender re-ran sender.py against a folder
    # whose contents (or --chunk-size) drifted from whatever produced the
    # in-progress transfer's numbering, so this upload's chunk indices
    # don't actually line up with the ones already collected.
    reset_note = None
    if accumulated_total is not None and video_total != accumulated_total:
        logger.info("New transfer detected (total %s -> %s); resetting progress.",
                    accumulated_total, video_total)
        reset_note = (
            f"⚠️ This upload reports {video_total} total chunk(s), but the "
            f"transfer in progress was tracking {accumulated_total} — treating "
            f"this as a different transfer. The {len(baseline)} chunk(s) already "
            f"collected were discarded (they don't correspond to the same "
            f"chunking). If that's not what you meant, make sure the sender's "
            f"folder contents and --chunk-size exactly match the original send."
        )
        reset_progress()

    accumulated_total = video_total
    accumulated_chunks.update(video_chunks)
    if video_hash:
        accumulated_hash = video_hash

    missing = [i for i in range(1, accumulated_total + 1) if i not in accumulated_chunks]
    if missing:
        raise IncompleteTransfer(missing, len(accumulated_chunks), accumulated_total, reset_note=reset_note)

    # Everything's here — reassemble, verify, write, zip
    await status.update(f"🔧 *Step 2/4* — Reassembling {accumulated_total} chunks…")
    reassembled_b45 = await loop.run_in_executor(
        None, reassemble_sync, accumulated_chunks, accumulated_total
    )

    if accumulated_hash:
        actual = hashlib.sha256(reassembled_b45.encode('ascii')).hexdigest()
        if actual != accumulated_hash:
            # Every chunk *looked* complete (each passed its own per-chunk
            # checksum) yet the whole payload still doesn't match -- treat
            # this transfer as unrecoverable rather than leaving the bad
            # "complete" state accumulated. Keeping it around used to mean
            # the next attempt's baseline was already 100% full of the bad
            # data: /status and the scan progress bar would show N/N the
            # instant the new video started (nothing left to count as
            # "new"), making a fresh retry look stuck rather than actually
            # restarting -- and it would keep re-merging over the same
            # already-corrupt entries instead of starting clean.
            expected_prefix = accumulated_hash[:12]
            reset_progress()
            raise ValueError(
                f"Checksum mismatch after reassembly "
                f"(expected {expected_prefix}…, got {actual[:12]}…). "
                f"Some chunk decoded incorrectly. Progress for this transfer "
                f"has been cleared — please re-record and resend the full video."
            )
    else:
        logger.warning("No checksum (HASH:) frame was captured — skipping integrity check.")

    json_str = base45.b45decode(reassembled_b45).decode('utf-8')

    # Step 3 — Write files
    await status.update("📂 *Step 3/4* — Writing files…")
    file_count = await loop.run_in_executor(None, write_files_sync, json_str, RECEIVED_DIR)

    # Step 4 — Zip
    await status.update(f"🗜 *Step 4/4* — Zipping {file_count} file(s)…")
    zip_path = await loop.run_in_executor(None, zip_received, RECEIVED_DIR, ARCHIVE_DIR)

    reset_progress()  # ready for the next transfer
    return zip_path, file_count


def scan_video_sync(video_path: Path, status: StatusMessage, loop: asyncio.AbstractEventLoop,
                     baseline: set[int] = frozenset()):
    """Blocking video scan — called in executor.

    `baseline` is the set of chunk indices already collected from earlier
    videos in this transfer (empty for a fresh transfer). It only affects
    what the progress bar *displays* -- reassembly and merging still happen
    in process_video regardless -- so a --resend video's progress reads as
    overall transfer completion instead of restarting from 0 every time.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    # Scale the frame-skip with capture fps. Higher fps gives you real extra
    # redundancy per QR code (more raw frames land inside each code's
    # on-screen window), but checking every single frame at 240fps would
    # make this step needlessly slow. These tiers grow slower than fps
    # itself, so you still gain redundancy at higher frame rates without
    # scan time going up 1:1 with it.
    if fps <= 60:
        STEP = 1
    elif fps <= 120:
        STEP = 2
    else:
        STEP = 3

    chunks      = {}
    total_exp   = None
    found_hash  = None
    # Texts already handled as of the most recently *checked* frame. A set
    # rather than a single value because the sender may show more than one
    # QR code per slide — zxing-cpp returns all of them per frame, and we
    # want to skip re-processing any of them while that same slide is still
    # on screen, not just the last one we happened to handle.
    last_texts   = set()
    frame_idx    = 0
    last_notify  = -1   # last combined-progress count we sent a status update for
    last_edit_at = 0.0  # time.monotonic() of the last progress-bar edit
    scan_start   = time.monotonic()
    new_found    = 0    # chunks this video contributed that weren't already in baseline
    rejected     = 0    # frames that decoded to a valid-looking chunk whose
                         # payload failed its own checksum -- discarded rather
                         # than trusted, so a later loop-pass (same video) or
                         # a follow-up video can still supply a clean read

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % STEP == 0:
            h, w = frame.shape[:2]
            if max(h, w) > MAX_SCAN_DIM:
                scale = MAX_SCAN_DIM / max(h, w)
                scan_frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            else:
                scan_frame = frame
            results = zxingcpp.read_barcodes(scan_frame)
            current_texts = set()
            for r in results:
                if not r.valid:
                    continue
                text = r.text.strip()
                current_texts.add(text)
                if text in last_texts:
                    continue

                hm = HASH_RE.match(text)
                if hm:
                    found_hash = hm.group(1)
                    continue

                m = CHUNK_RE.match(text)
                if not m:
                    continue

                idx, total, chunk_cs, payload = (
                    int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
                )

                # zxing-cpp can report a frame as "valid" even when video
                # compression / motion blur corrupted it past what the QR's
                # own error correction could recover -- especially likely at
                # ERROR_CORRECT_L. Checking the chunk's own checksum catches
                # that here, per-chunk, instead of only finding out via the
                # whole-payload hash at the very end of the transfer (see
                # sender.py's build_chunks() docstring). A rejected read is
                # simply not stored, so a cleaner read of the same index --
                # later in this same looping video, or in a follow-up video
                # -- can still fill it in normally.
                if not _chunk_checksum_ok(idx, chunk_cs, payload, "video"):
                    rejected += 1
                    continue

                if total_exp is None:
                    total_exp = total
                if idx not in chunks:
                    chunks[idx] = payload
                    if idx not in baseline:
                        new_found += 1

                # Combined = baseline (already collected from earlier videos
                # in this transfer) + newly found here -- so a --resend
                # video's bar reads as overall transfer progress, not "3/2624"
                # every time regardless of which indices it's contributing.
                combined = len(baseline) + new_found

                # Rate-limit progress-bar edits by wall-clock time, not chunk
                # count -- see PROGRESS_EDIT_INTERVAL above.
                now = time.monotonic()
                if combined != last_notify and now - last_edit_at >= PROGRESS_EDIT_INTERVAL:
                    last_notify  = combined
                    last_edit_at = now
                    pct  = int(combined / total_exp * 100)
                    fill = int(pct / 5)
                    bar  = "▓" * fill + "░" * (20 - fill)

                    extra = []
                    if baseline:
                        extra.append(f"+{new_found} new")
                    elapsed = now - scan_start
                    if elapsed >= 1.0 and new_found > 0:
                        extra.append(f"{new_found / elapsed:.1f}/s")
                    suffix = f" · {' · '.join(extra)}" if extra else ""

                    txt = (
                        f"🎬 *Step 1/4* — Scanning video frames…\n"
                        f"`[{bar}]` {combined}/{total_exp} chunks ({pct}%){suffix}"
                    )
                    asyncio.run_coroutine_threadsafe(status.update(txt), loop)

            last_texts = current_texts

        frame_idx += 1
        # Only stop early once we have every chunk *and* the checksum frame
        # (if the checksum genuinely never appears before the video runs
        # out, we still proceed below with a warning rather than blocking).
        if total_exp and len(chunks) == total_exp and found_hash is not None:
            break

    cap.release()
    if rejected:
        logger.info("Discarded %d chunk read(s) that failed their per-chunk checksum.", rejected)
    return chunks, total_exp, found_hash


def scan_image_sync(image_path: Path, baseline: set[int] = frozenset()):
    """Blocking single-photo scan — called in executor.

    Same chunk-parsing and per-chunk checksum verification as
    scan_video_sync, but for one still image instead of a video: there's
    no frame loop, no slide-transition handling, no early-break condition
    -- zxing-cpp just reads whatever barcodes are visible in the one
    frame. Useful for topping up a couple of missing chunks (see
    IncompleteTransfer's `--resend` suggestion): filming a video of a QR
    slide that never even changes is more fiddly than just snapping a
    photo of it. `baseline` is accepted for signature parity with
    scan_video_sync (process_video passes it either way) but isn't used
    here since there's no in-progress bar to render mid-scan.
    """
    frame = cv2.imread(str(image_path))
    if frame is None:
        raise RuntimeError(f"Cannot open image: {image_path}")

    h, w = frame.shape[:2]
    if max(h, w) > MAX_SCAN_DIM:
        scale = MAX_SCAN_DIM / max(h, w)
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    chunks     = {}
    total_exp  = None
    found_hash = None
    rejected   = 0

    for r in zxingcpp.read_barcodes(frame):
        if not r.valid:
            continue
        text = r.text.strip()

        hm = HASH_RE.match(text)
        if hm:
            found_hash = hm.group(1)
            continue

        m = CHUNK_RE.match(text)
        if not m:
            continue

        idx, total, chunk_cs, payload = (
            int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        )
        if not _chunk_checksum_ok(idx, chunk_cs, payload, "photo"):
            rejected += 1
            continue

        if total_exp is None:
            total_exp = total
        chunks[idx] = payload

    if rejected:
        logger.info("Discarded %d chunk read(s) that failed their per-chunk checksum (photo scan).", rejected)
    return chunks, total_exp, found_hash


def reassemble_sync(chunks: dict, total_exp: int) -> str:
    """Concatenate chunk payloads back into the full base45 text."""
    return "".join(chunks[i] for i in range(1, total_exp + 1))


def write_files_sync(json_str: str, out_dir: Path) -> int:
    """Clear out_dir, then write decoded files. Returns file count."""
    # Clear previous contents
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir()

    entries = json.loads(json_str)
    for entry in entries:
        rel_path = entry["p"]
        data     = base64.b64decode(entry["d"])
        dest     = out_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.info("  wrote: %s (%d bytes)", rel_path, len(data))

    return len(entries)


def zip_received(received_dir: Path, archive_dir: Path) -> Path:
    """Zip everything in received_dir into archive_dir. Returns zip path."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path  = archive_dir / f"transfer_{timestamp}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in received_dir.rglob("*"):
            if fpath.is_file():
                zf.write(fpath, fpath.relative_to(received_dir))

    logger.info("Zipped to: %s", zip_path)
    return zip_path


# ── Handlers ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    await update.message.reply_text(
        "👋 *QR Converter Bot* is ready!\n\n"
        "Send me a video file (`.mov` or `.mp4`) recorded from the QR slideshow. "
        "I'll decode it and send back a zip with all the files.\n\n"
        "You can also send a single *photo* of one QR frame — handy for "
        "topping up just a chunk or two without filming a whole video.\n\n"
        "Send several uploads back-to-back (or while one's still processing) "
        "and I'll queue them, working through them in order and merging "
        "chunks into the same transfer automatically.\n\n"
        "If an upload is missing some chunks, I'll keep what I've got — just "
        "send a top-up video or photo and it'll merge in.\n\n"
        "/status — check transfer + queue progress\n"
        "/reset — abandon the in-progress transfer\n"
        "/help — show this again",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    lines = []
    if accumulated_total is not None:
        lines.append(f"📦 Transfer in progress: {len(accumulated_chunks)}/{accumulated_total} chunks collected.")
        lines.append(f"`{render_coverage_bar(set(accumulated_chunks), accumulated_total)}`")
        missing = [i for i in range(1, accumulated_total + 1) if i not in accumulated_chunks]
        if missing:
            ranges = compress_ranges(missing)
            shown = ranges[:8]
            more  = len(ranges) - len(shown)
            lines.append("Missing: " + ", ".join(shown) + (
                f" (+{more} more range{'s' if more != 1 else ''})" if more else ""
            ))
    else:
        lines.append("No transfer in progress.")

    if is_busy:
        lines.append("🎬 Currently processing a video.")
    qsize = pending_videos.qsize()
    if qsize:
        lines.append(f"⏳ {qsize} video(s) queued behind it.")
    elif not is_busy:
        lines.append("Queue is empty.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    had_progress = accumulated_total is not None
    reset_progress()
    if had_progress:
        await update.message.reply_text("🔄 Cleared the in-progress transfer. Ready for a fresh one.")
    else:
        await update.message.reply_text("Nothing in progress — already clear.")


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    msg = update.message

    # A compressed photo (msg.photo) is a list of resolutions -- take the
    # largest. It's convenient for topping up just one or two missing
    # chunks (no need to film a video of a QR slide that never changes),
    # and Telegram's photo compression is fine here: a chunk that comes
    # out corrupted just fails its own per-chunk checksum and gets
    # skipped, same as a bad video frame would (see CHUNK_RE / scan_image_sync).
    if msg.photo:
        tg_obj = msg.photo[-1]
        fname  = f"photo_{msg.message_id}.jpg"
        kind   = "image"
    else:
        doc = msg.document or msg.video
        if not doc:
            await msg.reply_text(
                "Send a video file (`.mov`/`.mp4`, as a *file* for best quality) "
                "recorded from the QR slideshow, or a single *photo* of one QR "
                "frame to top up a chunk or two.",
                parse_mode="Markdown",
            )
            return

        fname = doc.file_name or f"video_{msg.message_id}.mp4"
        ext   = Path(fname).suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            kind = "video"
        elif ext in IMAGE_EXTENSIONS:
            kind = "image"
        else:
            await msg.reply_text(
                f"Unexpected file type: `{escape_backticks(fname)}`\n"
                f"Expected a video ({', '.join(VIDEO_EXTENSIONS)}) or an image "
                f"({', '.join(IMAGE_EXTENSIONS)}) of a QR frame.",
                parse_mode="Markdown",
            )
            return
        tg_obj = doc

    # Post an initial status message we'll edit throughout
    status_msg = await msg.reply_text(f"⬇️ Downloading `{escape_backticks(fname)}`…", parse_mode="Markdown")
    status     = StatusMessage(status_msg)
    # message_id keeps two queued uploads that happen to share a filename
    # from colliding on disk before the worker gets to either of them.
    media_path = TEMP_DIR / f"{msg.message_id}_{fname}"

    try:
        await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
        tg_file = await tg_obj.get_file()
        await tg_file.download_to_drive(str(media_path))
        size_mb = media_path.stat().st_size / 1_048_576
    except Exception as e:
        logger.exception("Download failed")
        await status.update(f"❌ Download failed: {e}")
        return

    # Queue for processing — video_worker() handles jobs one at a time, in
    # order, so several uploads (a transfer split across videos/photos, or
    # just sent in a burst) merge into the same transfer instead of the
    # later ones being rejected while an earlier one is still scanning.
    scan_label = "Scanning photo…" if kind == "image" else "Scanning video frames…"
    ahead = pending_videos.qsize() + (1 if is_busy else 0)
    if ahead == 0:
        await status.update(f"✅ Downloaded `{escape_backticks(fname)}` ({size_mb:.1f} MB)\n\n🎬 *Step 1/4* — {scan_label}")
    else:
        plural = "s" if ahead != 1 else ""
        await status.update(
            f"✅ Downloaded `{escape_backticks(fname)}` ({size_mb:.1f} MB)\n"
            f"⏳ Queued behind {ahead} upload{plural} — I'll start automatically once free."
        )

    await pending_videos.put(VideoJob(video_path=media_path, fname=fname, status=status, chat_id=msg.chat_id, kind=kind))


async def video_worker(bot):
    """Pulls queued videos one at a time and runs the full pipeline on each,
    so uploads never race each other over the shared accumulated_* state."""
    global is_busy
    while True:
        job = await pending_videos.get()
        is_busy = True
        try:
            zip_path, file_count = await process_video(job.video_path, job.status, kind=job.kind)

            await job.status.update(f"📤 Sending zip ({file_count} file(s))…")
            await bot.send_chat_action(chat_id=job.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            with open(zip_path, "rb") as f:
                await bot.send_document(
                    chat_id=job.chat_id,
                    document=f,
                    filename=zip_path.name,
                    caption=f"✅ Done! {file_count} file(s) packed into `{zip_path.name}`",
                    parse_mode="Markdown",
                )
            await job.status.update(f"✅ All done! Sent `{zip_path.name}` with {file_count} file(s).")

        except IncompleteTransfer as e:
            # Visual map of *where* the gaps are (a missing tail, a missing
            # head, scattered misses) -- much faster to read than a list of
            # numbers, especially for the common case of one big trailing gap.
            coverage = render_coverage_bar(set(accumulated_chunks), e.total)

            # Range-compressed display ("247-2624" instead of 2378 separate
            # numbers) -- readable regardless of how many chunks are missing.
            ranges = compress_ranges(e.missing)
            shown_ranges = ranges[:12]
            more_ranges  = len(ranges) - len(shown_ranges)
            ranges_display = ", ".join(shown_ranges) + (
                f" (+{more_ranges} more range{'s' if more_ranges != 1 else ''})" if more_ranges else ""
            )

            # Cap the --resend command itself, not just the display above --
            # a badly-scanned video can leave thousands of chunks missing,
            # and Telegram rejects any message over ~4096 chars outright.
            # Whatever doesn't fit just gets left for a follow-up video --
            # the queue already merges multiple uploads into one transfer.
            included, running_len = [], 0
            for i in e.missing:
                piece = str(i)
                added = len(piece) + (1 if included else 0)
                if running_len + added > MAX_RESEND_LIST_CHARS:
                    break
                included.append(piece)
                running_len += added
            missing_str = ",".join(included)
            leftover = len(e.missing) - len(included)
            resend_note = (
                f"\n(+{leftover} more after that — send a follow-up video for those once "
                f"this one's merged in.)" if leftover else ""
            )
            reset_prefix = f"{e.reset_note}\n\n" if e.reset_note else ""
            await job.status.update(
                f"{reset_prefix}"
                f"📦 Got {e.found}/{e.total} chunks so far — progress saved.\n"
                f"`{coverage}`\n"
                f"Missing: {ranges_display}\n\n"
                f"On the sender PC, run:\n"
                f"`python sender.py --resend {missing_str}`{resend_note}\n"
                f"and send me that (shorter) video — or, for just a chunk or two, "
                f"a single *photo* of the QR frame works too. No need to redo the "
                f"whole thing.\n\n"
                f"Or send /reset to abandon this transfer and start over."
            )

        except Exception as e:
            logger.exception("Processing failed")
            await job.status.update(f"❌ Error: {e}")

        finally:
            is_busy = False
            if job.video_path.exists():
                job.video_path.unlink()
            pending_videos.task_done()


# ── Main ───────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    # Runs once, in the app's own event loop, before polling starts.
    app.create_task(video_worker(app.bot), name="video_worker")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))
    # Catch document, video, and photo message types
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.PHOTO, handle_media))

    logger.info("Converter bot running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
