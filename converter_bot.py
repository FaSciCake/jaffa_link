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

CHUNK_RE = re.compile(r'^(\d+)/(\d+):(.+)$')
HASH_RE  = re.compile(r'^HASH:([0-9a-f]{64})$')

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


def escape_backticks(text: str) -> str:
    """Defang literal backticks in untrusted text (e.g. a phone's filename)
    so it can't break out of a Markdown code span and crash the send."""
    return text.replace('`', "'")


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
    def __init__(self, missing: list[int], found: int, total: int):
        self.missing = missing
        self.found   = found
        self.total   = total
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
        if text == self._last and not force:
            return
        try:
            self._msg = await self._msg.edit_text(text, parse_mode=parse_mode)
            self._last = text
        except TelegramError:
            pass  # silently ignore edit failures (e.g. message too old)


# ── Core processing ────────────────────────────────────────────────────────────

async def process_video(video_path: Path, status: StatusMessage) -> tuple[Path, int]:
    """
    Full pipeline for one uploaded video: scan → merge into the running
    transfer → (if complete) verify checksum → reassemble → write → zip.

    Raises IncompleteTransfer if chunks are still missing after merging
    this video's contribution (progress is kept for the next upload).
    """
    global accumulated_chunks, accumulated_total, accumulated_hash
    loop = asyncio.get_event_loop()

    # Step 1 — Scan video frames
    await status.update("🎬 *Step 1/4* — Scanning video frames…")
    video_chunks, video_total, video_hash = await loop.run_in_executor(
        None, scan_video_sync, video_path, status, loop
    )

    if not video_chunks or video_total is None:
        raise ValueError("No QR chunks found in the video. Is this the right file?")

    # If this video reports a different total than what's accumulated so
    # far, treat it as a new/different transfer rather than mixing the two.
    if accumulated_total is not None and video_total != accumulated_total:
        logger.info("New transfer detected (total %s -> %s); resetting progress.",
                    accumulated_total, video_total)
        reset_progress()

    accumulated_total = video_total
    accumulated_chunks.update(video_chunks)
    if video_hash:
        accumulated_hash = video_hash

    missing = [i for i in range(1, accumulated_total + 1) if i not in accumulated_chunks]
    if missing:
        raise IncompleteTransfer(missing, len(accumulated_chunks), accumulated_total)

    # Everything's here — reassemble, verify, write, zip
    await status.update(f"🔧 *Step 2/4* — Reassembling {accumulated_total} chunks…")
    reassembled_b45 = await loop.run_in_executor(
        None, reassemble_sync, accumulated_chunks, accumulated_total
    )

    if accumulated_hash:
        actual = hashlib.sha256(reassembled_b45.encode('ascii')).hexdigest()
        if actual != accumulated_hash:
            raise ValueError(
                f"Checksum mismatch after reassembly "
                f"(expected {accumulated_hash[:12]}…, got {actual[:12]}…). "
                f"Some chunk decoded incorrectly — please re-record and try again."
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


def scan_video_sync(video_path: Path, status: StatusMessage, loop: asyncio.AbstractEventLoop):
    """Blocking video scan — called in executor."""
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
        STEP = 2
    elif fps <= 120:
        STEP = 3
    else:
        STEP = 5

    chunks      = {}
    total_exp   = None
    found_hash  = None
    # Texts already handled as of the most recently *checked* frame. A set
    # rather than a single value because the sender may show more than one
    # QR code per slide — zxing-cpp returns all of them per frame, and we
    # want to skip re-processing any of them while that same slide is still
    # on screen, not just the last one we happened to handle.
    last_texts  = set()
    frame_idx   = 0
    last_notify = -1  # last chunk count we sent a status update for

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % STEP == 0:
            results = zxingcpp.read_barcodes(frame)
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

                idx, total, payload = int(m.group(1)), int(m.group(2)), m.group(3)
                if total_exp is None:
                    total_exp = total
                if idx not in chunks:
                    chunks[idx] = payload

                # Send a status update every 5 new chunks (rate-limit edits)
                if len(chunks) % 5 == 0 and len(chunks) != last_notify:
                    last_notify = len(chunks)
                    pct  = int(len(chunks) / total_exp * 100)
                    fill = int(pct / 5)
                    bar  = "▓" * fill + "░" * (20 - fill)
                    txt  = (
                        f"🎬 *Step 1/4* — Scanning video frames…\n"
                        f"`[{bar}]` {len(chunks)}/{total_exp} chunks ({pct}%)"
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
        "Send several videos back-to-back (or while one's still processing) "
        "and I'll queue them, working through them in order and merging "
        "chunks into the same transfer automatically.\n\n"
        "If a video is missing some chunks, I'll keep what I've got — just "
        "send a top-up video and it'll merge in.\n\n"
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
    else:
        lines.append("No transfer in progress.")

    if is_busy:
        lines.append("🎬 Currently processing a video.")
    qsize = pending_videos.qsize()
    if qsize:
        lines.append(f"⏳ {qsize} video(s) queued behind it.")
    elif not is_busy:
        lines.append("Queue is empty.")

    await update.message.reply_text("\n".join(lines))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    had_progress = accumulated_total is not None
    reset_progress()
    if had_progress:
        await update.message.reply_text("🔄 Cleared the in-progress transfer. Ready for a fresh one.")
    else:
        await update.message.reply_text("Nothing in progress — already clear.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    # Only accept document uploads (phone videos sent as files, not compressed)
    msg = update.message
    doc = msg.document or msg.video
    if not doc:
        await msg.reply_text("Please send the video as a *file* (not compressed). Use 'Send as File' in Telegram.", parse_mode="Markdown")
        return

    # Check extension loosely
    fname = doc.file_name or f"video_{msg.message_id}.mp4"
    if not any(fname.lower().endswith(ext) for ext in (".mov", ".mp4", ".avi", ".mkv")):
        await msg.reply_text(f"Unexpected file type: `{escape_backticks(fname)}`\nExpected a .mov or .mp4 video.", parse_mode="Markdown")
        return

    # Post an initial status message we'll edit throughout
    status_msg = await msg.reply_text(f"⬇️ Downloading `{escape_backticks(fname)}`…", parse_mode="Markdown")
    status     = StatusMessage(status_msg)
    # message_id keeps two queued uploads that happen to share a filename
    # from colliding on disk before the worker gets to either of them.
    video_path = TEMP_DIR / f"{msg.message_id}_{fname}"

    try:
        await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(video_path))
        size_mb = video_path.stat().st_size / 1_048_576
    except Exception as e:
        logger.exception("Download failed")
        await status.update(f"❌ Download failed: {e}")
        return

    # Queue for processing — video_worker() handles jobs one at a time, in
    # order, so several uploads (a transfer split across videos, or just
    # sent in a burst) merge into the same transfer instead of the later
    # ones being rejected while an earlier one is still scanning.
    ahead = pending_videos.qsize() + (1 if is_busy else 0)
    if ahead == 0:
        await status.update(f"✅ Downloaded `{escape_backticks(fname)}` ({size_mb:.1f} MB)\n\n🎬 *Step 1/4* — Scanning video frames…")
    else:
        plural = "s" if ahead != 1 else ""
        await status.update(
            f"✅ Downloaded `{escape_backticks(fname)}` ({size_mb:.1f} MB)\n"
            f"⏳ Queued behind {ahead} video{plural} — I'll start automatically once free."
        )

    await pending_videos.put(VideoJob(video_path=video_path, fname=fname, status=status, chat_id=msg.chat_id))


async def video_worker(bot):
    """Pulls queued videos one at a time and runs the full pipeline on each,
    so uploads never race each other over the shared accumulated_* state."""
    global is_busy
    while True:
        job = await pending_videos.get()
        is_busy = True
        try:
            zip_path, file_count = await process_video(job.video_path, job.status)

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
            shown = e.missing[:20]
            more  = f" (+{len(e.missing) - 20} more)" if len(e.missing) > 20 else ""
            missing_str = ",".join(str(i) for i in e.missing)
            await job.status.update(
                f"📦 Got {e.found}/{e.total} chunks so far — progress saved.\n"
                f"Still missing {len(e.missing)}: {shown}{more}\n\n"
                f"On the sender PC, run:\n"
                f"`python sender.py --resend {missing_str}`\n"
                f"and send me that (shorter) video — no need to redo the whole thing.\n\n"
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
    # Catch both document and video message types
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO, handle_video))

    logger.info("Converter bot running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
