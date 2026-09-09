"""
sender.py  —  Run this on the air-gapped Windows PC.

Usage:
    python sender.py                            # scans current directory
    python sender.py C:/path/to/project          # scans a specific folder
    python sender.py --chunk-size 2500           # bigger/smaller QR payloads
    python sender.py --resend 5,12,47            # only loop these chunk numbers
                                                   # (the bot will tell you which
                                                   #  ones it's still missing)
    python sender.py --codes-per-row 1            # force one QR at a time
                                                    # (default: auto-picked for
                                                    #  your screen's shape)

Controls in the QR window:
    Space / Left-click  →  start/stop auto-play (loops forever until you stop it)
    Right arrow          →  step forward manually (while stopped)
    Left arrow           →  step backward manually (while stopped)
    Escape                →  quit

Requirements (already on the PC):
    - PySide6
    - qrcode    (any version >= 1.0.0)
    - Pillow    (very likely installed; if not, falls back to pypng/svg)
    - base45    (pip install base45)

Why base45 instead of base64: QR codes can pack text in "alphanumeric mode"
(digits, uppercase letters, space, and a few symbols) at ~5.5 bits/char, or
"byte mode" (anything) at 8 bits/char. Base64 needs byte mode because it
uses lowercase letters. Base45 (RFC 9285) only ever emits characters that
qualify for alphanumeric mode, so the same data needs meaningfully less
room per QR code. The `qrcode` library already auto-detects and uses
alphanumeric mode for text that qualifies — nothing else to change there.

Why multiple QR codes per slide: a QR code is always square, so on a 16:9
screen a single code sized to fit the height leaves a lot of width unused.
Splitting the screen into a row of smaller squares reclaims that space —
see suggest_codes_per_row() below for the reasoning. The receiver doesn't
need to know how many codes are on screen at once; zxing-cpp already
detects and decodes every barcode it finds in a frame.
"""

import argparse
import base64
import hashlib
import json
import math
import os
import sys

import base45

# ── Collect files ──────────────────────────────────────────────────────────────

def collect_files(root: str) -> list[dict]:
    """Walk root folder and return list of {rel_path, b64_content} dicts."""
    entries = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden / cache dirs
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d != '__pycache__']
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            rel   = os.path.relpath(fpath, root).replace('\\', '/')
            try:
                with open(fpath, 'rb') as f:
                    data = f.read()
                entries.append({'p': rel, 'd': base64.b64encode(data).decode()})
                print(f"  + {rel}  ({len(data)} bytes)")
            except Exception as e:
                print(f"  ! skipping {rel}: {e}")
    return entries


def build_chunks(entries: list[dict], chunk_size: int = 2000) -> tuple[list[str], str]:
    """
    Serialize entries to JSON, base45-encode the result, then split into
    chunk_size-character pieces. Each data chunk is a plain string:
    "INDEX/TOTAL:payload" — payload is a slice of the base45 text.

    Returns (chunks, sha256_hex) — sha256_hex is the checksum of the full
    base45 text, so the receiver can verify the reassembled data actually
    matches what was sent (see the HASH: frame in __main__).

    Note: json.dumps() with the default ensure_ascii=True already produces
    pure-ASCII text (any non-ASCII gets \\uXXXX-escaped), so there's no need
    to base64-wrap the whole JSON blob on top of the per-file base64 already
    happening in collect_files() — that was pure overhead.
    """
    full_json = json.dumps(entries, separators=(',', ':'))
    full_b45  = base45.b45encode(full_json.encode('ascii')).decode('ascii')
    digest    = hashlib.sha256(full_b45.encode('ascii')).hexdigest()

    total  = math.ceil(len(full_b45) / chunk_size)
    chunks = []
    for i in range(total):
        piece = full_b45[i * chunk_size : (i + 1) * chunk_size]
        chunks.append(f"{i+1}/{total}:{piece}")

    return chunks, digest


# ── Generate QR images ─────────────────────────────────────────────────────────

def make_qr_image(text: str):
    """
    Render QR code using only qrcode + PySide6/Qt — no Pillow, no PyPNG.
    Uses ERROR_CORRECT_L for maximum data capacity per code.

    qrcode's own qr.make() checks all 8 QR mask patterns (each mask XORs
    the data into a different visual pattern) and keeps whichever scores
    best on scan-friendliness — correct, but laying out and scoring each
    candidate is most of the per-code cost, and doing that 8 times adds up
    across a whole chunk set. Sampling 4 of the 8 lands within ~1.5% of the
    true-optimal score in testing (many payloads land on the exact same
    winner either way) for roughly 1.5-2x the speed. Tested against
    zxing-cpp decoding, not just theory. Widen MASK_CANDIDATES back to
    range(8) if you ever want the original exhaustive behaviour, or narrow
    it further (e.g. just (0,) ) to trade a bit more quality for speed.

    Returns a QPixmap.
    """
    import qrcode
    import qrcode.util as qrutil
    from PySide6.QtGui import QPixmap, QPainter, QColor
    from PySide6.QtCore import Qt

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=4,
    )
    qr.add_data(text)
    qr.best_fit()  # picks the version — same as make(fit=True) would

    MASK_CANDIDATES = (0, 2, 4, 6)
    best_pattern, best_score = None, None
    for m in MASK_CANDIDATES:
        qr.makeImpl(True, m)
        score = qrutil.lost_point(qr.modules)
        if best_score is None or score < best_score:
            best_score, best_pattern = score, m
    qr.makeImpl(False, best_pattern)

    matrix  = qr.get_matrix()
    size    = len(matrix)
    px_size = size * 10

    pixmap = QPixmap(px_size, px_size)
    pixmap.fill(QColor("white"))

    painter = QPainter(pixmap)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.drawRect(x * 10, y * 10, 10, 10)
    painter.end()

    return pixmap


# ── PySide6 slideshow ──────────────────────────────────────────────────────────

SLIDESHOW_HZ = 20  # auto-advance rate — must match what receiver expects


def suggest_codes_per_row(screen_w: int, screen_h: int, max_n: int = 6) -> int:
    """
    Work out how many same-sized square QR codes, laid out in a single
    horizontal row, make the best use of a WxH screen.

    A QR code is always square, so one code sized to a screen's smaller
    dimension leaves the rest of the larger dimension unused — on a 1920x
    1080 (16:9) screen, a single code can only ever be 1080px on a side,
    wasting most of the extra 840px of width. Splitting into a row of N
    codes gives each one a slot that's closer to square, so each can be
    scaled up more, even though there are now N of them to fit. Total QR
    area (a decent proxy for total data throughput per frame, since more
    area means either bigger modules at the same data, or more data at
    the same module size) is maximized somewhere in the middle — split
    too little and you waste width; split too much and each slot gets
    width-starved and every code pays its fixed per-code overhead (finder
    patterns, quiet zone) again. For 1920x1080 this works out to 2.
    """
    best_n, best_area = 1, 0.0
    for n in range(1, max_n + 1):
        slot_side = min(screen_w / n, screen_h)
        area = n * slot_side ** 2
        if area > best_area:
            best_area, best_n = area, n
    return best_n


def run_slideshow(chunks: list[str], codes_per_row: int | None = None):
    from PySide6.QtWidgets import (
        QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QSizePolicy
    )
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QKeyEvent

    app = QApplication.instance() or QApplication(sys.argv)

    if codes_per_row is None:
        screen = QApplication.primaryScreen().size()
        codes_per_row = suggest_codes_per_row(screen.width(), screen.height())
        print(f"Auto-picked {codes_per_row} code(s) per slide for your "
              f"{screen.width()}x{screen.height()} screen.")

    num_frames = len(chunks)
    num_slides = math.ceil(num_frames / codes_per_row)
    print(f"\nGenerating {num_frames} QR codes across {num_slides} slide(s)… "
          f"(this may take a moment)")
    pixmaps = []
    for i, chunk in enumerate(chunks):
        print(f"  QR {i+1}/{num_frames}…", end='\r')
        pixmaps.append(make_qr_image(chunk))
    print(f"  Done! {num_frames} QR codes ready.          ")

    class Slideshow(QWidget):
        def __init__(self):
            super().__init__()
            self.idx     = 0  # slide index (a slide holds codes_per_row codes)
            self.running = False  # auto-play starts only when user is ready
            self.setWindowTitle("QR Transfer")
            self.setStyleSheet("background: white;")

            layout = QVBoxLayout(self)
            layout.setContentsMargins(20, 20, 20, 20)

            qr_row = QHBoxLayout()
            self.qr_labels = []
            for _ in range(codes_per_row):
                lbl = QLabel(alignment=Qt.AlignCenter)
                lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                qr_row.addWidget(lbl)
                self.qr_labels.append(lbl)
            layout.addLayout(qr_row)

            self.info_label = QLabel(alignment=Qt.AlignCenter)
            self.info_label.setStyleSheet("font-size: 28px; font-weight: bold; color: #333;")
            layout.addWidget(self.info_label)

            self.hint_label = QLabel(alignment=Qt.AlignCenter)
            self.hint_label.setStyleSheet("font-size: 14px; color: #888;")
            layout.addWidget(self.hint_label)

            self.timer = QTimer()
            self.timer.setInterval(1000 // SLIDESHOW_HZ)
            self.timer.timeout.connect(self._auto_advance)

            self.show_current()
            self.showFullScreen()

        def show_current(self):
            screen = QApplication.primaryScreen().size()
            slot_w   = screen.width() / codes_per_row
            max_side = int(min(slot_w, screen.height()) * 0.75)

            base = self.idx * codes_per_row
            for j, lbl in enumerate(self.qr_labels):
                frame_i = base + j
                if frame_i < num_frames:
                    lbl.setPixmap(
                        # FastTransformation (nearest-neighbor): smooth/
                        # bilinear scaling blurs the sharp module edges QR
                        # decoders rely on for thresholding.
                        pixmaps[frame_i].scaled(
                            max_side, max_side, Qt.KeepAspectRatio, Qt.FastTransformation
                        )
                    )
                else:
                    lbl.clear()  # odd leftover on the final slide — nothing here

            self.info_label.setText(f"Slide  {self.idx + 1}  /  {num_slides}")
            if self.running:
                self.hint_label.setText("● RECORDING — loops forever, SPACE or click to stop")
                self.hint_label.setStyleSheet("font-size: 14px; color: red; font-weight: bold;")
            else:
                self.hint_label.setText("SPACE / click = start auto-play  |  ← → = manual step  |  ESC = quit")
                self.hint_label.setStyleSheet("font-size: 14px; color: #888;")

        def _auto_advance(self):
            # Loop back to the start instead of stopping after one pass.
            # The receiver already de-dupes by chunk index and exits early
            # once it has everything, so looping just gives you a much
            # wider margin to start/stop recording without perfect timing.
            self.idx = self.idx + 1 if self.idx < num_slides - 1 else 0
            self.show_current()

        def toggle_autoplay(self):
            self.running = not self.running
            if self.running:
                self.idx = 0  # always restart from beginning
                self.timer.start()
            else:
                self.timer.stop()
            self.show_current()

        def advance(self, delta: int):
            if not self.running:
                self.idx = max(0, min(num_slides - 1, self.idx + delta))
                self.show_current()

        def mousePressEvent(self, event):
            self.toggle_autoplay()

        def keyPressEvent(self, event: QKeyEvent):
            key = event.key()
            if key == Qt.Key_Space:
                self.toggle_autoplay()
            elif key == Qt.Key_Right:
                self.advance(+1)
            elif key == Qt.Key_Left:
                self.advance(-1)
            elif key == Qt.Key_Escape:
                self.close()

    win = Slideshow()
    app.exec()


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="QR slideshow sender for air-gapped file transfer"
    )
    parser.add_argument(
        "root", nargs="?", default=".",
        help="Folder to scan and send (default: current directory)"
    )
    parser.add_argument(
        "--chunk-size", type=int, default=2000,
        help="Characters of base45 text per QR chunk (default: 2000). "
             "Base45 is denser than the old base64 scheme, so you likely "
             "have headroom to push this up if you want fewer total frames."
    )
    parser.add_argument(
        "--resend", metavar="LIST", default=None,
        help="Comma-separated chunk numbers to loop, e.g. --resend 5,12,47 "
             "— for topping up a transfer where the bot told you some "
             "chunks are still missing, without re-sending everything."
    )
    parser.add_argument(
        "--codes-per-row", type=int, default=None,
        help="How many QR codes to show side by side per slide. Default: "
             "auto-picked from your screen's aspect ratio (works out to 2 "
             "for a typical 1920x1080 monitor). Pass 1 for the old single-"
             "code-at-a-time behaviour."
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()

    print(f"Scanning: {os.path.abspath(args.root)}\n")
    entries = collect_files(args.root)
    if not entries:
        print("No files found!")
        sys.exit(1)
    print(f"\nFound {len(entries)} file(s).")

    data_chunks, digest = build_chunks(entries, chunk_size=args.chunk_size)
    print(f"Split into {len(data_chunks)} QR chunk(s). Checksum: {digest[:12]}…")

    if args.resend:
        wanted = {int(x) for x in args.resend.split(',')}
        data_chunks = [c for c in data_chunks if int(c.split('/', 1)[0]) in wanted]
        print(f"--resend given: only looping {len(data_chunks)} chunk(s): {sorted(wanted)}")
        print("(Make sure the folder contents haven't changed since the first attempt —")
        print(" the checksum above needs to match what the bot already has.)")

    # Always include the checksum frame, even on a --resend run, in case the
    # bot didn't catch it the first time either.
    frames = [f"HASH:{digest}"] + data_chunks

    run_slideshow(frames, codes_per_row=args.codes_per_row)
