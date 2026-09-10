"""
End-to-end test of the real sender.py / converter_bot.py functions,
minus the actual camera/QR/video layer (that part is unchanged logic
and can't be exercised without real hardware). This stubs out
scan_video_sync's video-reading with synthetic chunk sets that were
built by parsing real chunk strings through the real regexes, so
everything downstream (merging, checksum, base45, JSON, file writing,
zipping) runs for real.
"""
import asyncio
import filecmp
import hashlib
import os
import random
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import sender
import converter_bot as cb


def render_qr_frame_image(texts: list[str], box: int = 6, gap: int = 20, border: int = 4) -> np.ndarray:
    """Render one or more QR codes side by side into a single grayscale-ish
    BGR image, mimicking a multi-code slide -- without needing PySide6 or
    Pillow (neither is a hard dependency of the receiving side). Uses the
    same qrcode.QRCode + get_matrix() approach sender.py's make_qr_image()
    uses, just rasterized with numpy/cv2 instead of Qt."""
    import qrcode

    imgs = []
    for text in texts:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=1, border=border)
        qr.add_data(text)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        size = len(matrix)
        arr = np.full((size, size), 255, dtype=np.uint8)
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    arr[y, x] = 0
        arr = cv2.resize(arr, (size * box, size * box), interpolation=cv2.INTER_NEAREST)
        imgs.append(arr)

    h = max(im.shape[0] for im in imgs)
    total_w = sum(im.shape[1] for im in imgs) + gap * (len(imgs) - 1)
    canvas = np.full((h, total_w), 255, dtype=np.uint8)
    x = 0
    for im in imgs:
        canvas[0:im.shape[0], x:x + im.shape[1]] = im
        x += im.shape[1] + gap
    return cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)


class FakeStatus:
    async def update(self, text, force=False):
        pass


def parse_frames_to_scan_result(frames):
    """Mimic what scan_video_sync's inner loop does, using the real regexes
    -- including rejecting a chunk whose payload fails its own per-chunk
    checksum, same as a corrupted-but-"valid" QR read would be rejected."""
    chunks = {}
    total_exp = None
    found_hash = None
    for text in frames:
        hm = cb.HASH_RE.match(text)
        if hm:
            found_hash = hm.group(1)
            continue
        m = cb.CHUNK_RE.match(text)
        if not m:
            continue
        idx, total, chunk_cs, payload = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        if hashlib.sha256(payload.encode('ascii')).hexdigest()[:8] != chunk_cs:
            continue
        if total_exp is None:
            total_exp = total
        chunks[idx] = payload
    return chunks, total_exp, found_hash


async def main():
    # ---- 1. Build a realistic test source folder -------------------------
    src = Path(tempfile.mkdtemp(prefix="qrtest_src_"))
    (src / "sub").mkdir()
    (src / "readme.md").write_text("# Hello\nThis is a test file with unicode: héllo wörld 日本語\n")
    (src / "sub" / "data.bin").write_bytes(bytes(random.randrange(256) for _ in range(5000)))
    (src / "sub" / "notes.txt").write_text("plain ascii notes\nline two\n")
    (src / "empty_ish.txt").write_text("x")

    print(f"Source folder: {src}")
    for p in sorted(src.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(src)}  ({p.stat().st_size} bytes)")

    # ---- 2. Sender-side: collect + build chunks (REAL functions) ---------
    entries = sender.collect_files(str(src))
    data_chunks, digest = sender.build_chunks(entries, chunk_size=120)  # small chunk_size to force many chunks
    print(f"\nBuilt {len(data_chunks)} data chunks, digest={digest[:12]}...")
    assert len(data_chunks) >= 4, "test needs several chunks to be meaningful"

    all_frames = [f"HASH:{digest}"] + data_chunks

    # ---- 3. Simulate "video 1": drop a few chunks (and even drop the HASH
    #         frame, to test the soft-fallback path) ----------------------
    cb.reset_progress()
    dropped_indices = {2, 5}
    video1_frames = [f for f in data_chunks
                      if int(f.split('/', 1)[0]) not in dropped_indices]
    # deliberately do NOT include the HASH frame in video 1
    v1_chunks, v1_total, v1_hash = parse_frames_to_scan_result(video1_frames)
    assert v1_hash is None, "video 1 should not have captured the hash frame"

    cb.scan_video_sync = lambda video_path, status, loop, baseline=frozenset(): (v1_chunks, v1_total, v1_hash)

    status = FakeStatus()
    dummy_path = Path("/tmp/does_not_need_to_exist.mp4")

    try:
        await cb.process_video(dummy_path, status)
        raise SystemExit("FAIL: expected IncompleteTransfer on video 1, got success")
    except cb.IncompleteTransfer as e:
        got_missing = set(e.missing)
        print(f"\nVideo 1 -> IncompleteTransfer as expected. Missing: {sorted(got_missing)}")
        assert got_missing == dropped_indices, f"expected missing={dropped_indices}, got {got_missing}"
    print("Accumulated so far:", len(cb.accumulated_chunks), "/", cb.accumulated_total,
          "hash captured:", cb.accumulated_hash is not None)
    assert cb.accumulated_hash is None  # confirms the soft-fallback path is live mid-transfer

    # ---- 4. Simulate the --resend video: only the missing indices, PLUS
    #         the HASH frame (mirrors what sender.py --resend now sends) --
    resend_wanted = dropped_indices
    resend_frames = [f"HASH:{digest}"] + [
        f for f in data_chunks if int(f.split('/', 1)[0]) in resend_wanted
    ]
    v2_chunks, v2_total, v2_hash = parse_frames_to_scan_result(resend_frames)
    cb.scan_video_sync = lambda video_path, status, loop, baseline=frozenset(): (v2_chunks, v2_total, v2_hash)

    zip_path, file_count = await cb.process_video(dummy_path, status)
    print(f"\nVideo 2 (resend) -> success. {file_count} files, zip at {zip_path}")
    assert cb.accumulated_total is None, "progress should reset after a completed transfer"

    # ---- 5. Verify output matches the source, byte-for-byte --------------
    extract_dir = Path(tempfile.mkdtemp(prefix="qrtest_out_"))
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    cmp = filecmp.dircmp(src, extract_dir)
    assert not cmp.left_only and not cmp.right_only, f"file set mismatch: {cmp.left_only} vs {cmp.right_only}"
    _, mismatch, errors = filecmp.cmpfiles(src, extract_dir,
                                            [p.name for p in src.iterdir() if p.is_file()], shallow=False)
    same_sub, mismatch_sub, errors_sub = filecmp.cmpfiles(
        src / "sub", extract_dir / "sub",
        [p.name for p in (src / "sub").iterdir()], shallow=False)
    assert not mismatch and not errors, f"top-level mismatch: {mismatch} errors: {errors}"
    assert not mismatch_sub and not errors_sub, f"sub mismatch: {mismatch_sub} errors: {errors_sub}"
    print("Byte-for-byte match: PASS (all files, including binary + unicode content)")

    # ---- 6. Corruption detection: mutate one payload character and verify
    #         the checksum mismatch is caught -----------------------------
    cb.reset_progress()
    corrupt_chunks = dict(v1_chunks)  # reuse video1's parsed chunks
    # also need the ones from resend to have a complete set, then corrupt one
    full_chunks, full_total, _ = parse_frames_to_scan_result(data_chunks)
    corrupt_chunks = dict(full_chunks)
    some_idx = next(iter(corrupt_chunks))
    original_payload = corrupt_chunks[some_idx]
    flipped = ('A' if original_payload[0] != 'A' else 'B') + original_payload[1:]
    corrupt_chunks[some_idx] = flipped

    cb.scan_video_sync = lambda video_path, status, loop, baseline=frozenset(): (corrupt_chunks, full_total, digest)
    try:
        await cb.process_video(dummy_path, status)
        raise SystemExit("FAIL: expected checksum ValueError on corrupted chunk, got success")
    except ValueError as e:
        assert "Checksum mismatch" in str(e), f"wrong error: {e}"
        print(f"\nCorruption test -> correctly caught: {e}")
    assert cb.accumulated_total is None, (
        "progress must be cleared after a checksum-mismatch failure -- otherwise "
        "the next resend starts from a baseline that's already 'full' of the bad "
        "data, and its scan progress / \"/status\" reads as stuck at 100% instead "
        "of restarting cleanly"
    )
    print("Progress correctly cleared after checksum failure.")

    # ---- 7. Per-chunk checksum: a frame whose payload was altered in transit
    #         (simulating a QR misread that zxing-cpp still reported as
    #         "valid") must be discarded without being stored -- and a later,
    #         clean read of the same chunk index (the point of the slideshow
    #         looping) must still be able to fill it in normally. -----------
    sample_chunk               = data_chunks[0]
    idx_str, rest              = sample_chunk.split('/', 1)
    total_str, cs_and_payload  = rest.split('/', 1)
    cs_str, good_payload       = cs_and_payload.split(':', 1)
    bad_payload = ('A' if good_payload[0] != 'A' else 'B') + good_payload[1:]
    bad_frame   = f"{idx_str}/{total_str}/{cs_str}:{bad_payload}"  # checksum now stale

    only_bad_chunks, _, _ = parse_frames_to_scan_result([bad_frame])
    assert int(idx_str) not in only_bad_chunks, \
        "a chunk whose payload fails its own checksum must be discarded, not stored"

    recovered_chunks, _, _ = parse_frames_to_scan_result([bad_frame, sample_chunk])
    assert recovered_chunks.get(int(idx_str)) == good_payload, \
        "a later clean read of the same chunk index should still be accepted"
    print("Per-chunk checksum test -> corrupted read rejected, later clean read accepted")

    # ---- 8. Real image-scan integration test: render actual QR codes (not
    #         mocked) for the HASH frame + one data chunk into a single PNG
    #         -- as if photographing one multi-code slide -- and run them
    #         through the real scan_image_sync. Exercises the actual
    #         qrcode-encode -> zxing-cpp-decode round trip for the new
    #         photo-upload path, not just regex parsing of pre-made strings.
    img_dir  = Path(tempfile.mkdtemp(prefix="qrtest_img_"))
    img_path = img_dir / "frame.png"
    img = render_qr_frame_image([f"HASH:{digest}", sample_chunk])
    cv2.imwrite(str(img_path), img)

    img_chunks, img_total, img_hash = cb.scan_image_sync(img_path)
    sample_idx     = int(idx_str)
    sample_payload = good_payload
    assert img_hash == digest, "HASH frame should be read from the photo"
    assert img_total == full_total, f"expected total={full_total}, got {img_total}"
    assert img_chunks.get(sample_idx) == sample_payload, \
        "chunk payload decoded from the photo should match what was encoded"
    print(f"\nImage-scan test -> real QR encode/decode round trip via photo succeeded "
          f"(chunk {sample_idx}, hash captured)")

    # ---- 9. sender.py --resend state validation: catches a folder whose
    #         contents drifted since the last full send, even in cases a
    #         plain chunk-count range check can't -- e.g. the folder shrank
    #         by one file but the requested index still happens to fall
    #         within the (now-different) total. ---------------------------
    state_dir   = Path(tempfile.mkdtemp(prefix="qrtest_state_"))
    payload_dir = state_dir / "payload"
    payload_dir.mkdir()
    (payload_dir / "only.txt").write_text("original content for state test\n")
    state_path = str(state_dir / "sender_state.json")

    orig_entries  = sender.collect_files(str(payload_dir))
    orig_chunks, orig_digest = sender.build_chunks(orig_entries, chunk_size=40)
    orig_manifest = sender.file_manifest(orig_entries)
    sender.save_send_state(str(payload_dir), 40, len(orig_chunks), orig_digest, orig_manifest, state_path=state_path)

    loaded = sender.load_send_state(state_path)
    assert loaded is not None and loaded["digest"] == orig_digest
    assert sender.diff_send_state(loaded, str(payload_dir), 40, orig_manifest) == [], \
        "unchanged folder/settings should report no problems"

    # Change the folder contents (mirroring what apparently happened between
    # the user's original send and their later --resend run) and confirm
    # the mismatch is caught with a specific diagnosis, not silently.
    (payload_dir / "only.txt").write_text("different content now\n")
    changed_manifest = sender.file_manifest(sender.collect_files(str(payload_dir)))
    problems = sender.diff_send_state(loaded, str(payload_dir), 40, changed_manifest)
    assert problems and any("only.txt" in p for p in problems), \
        f"expected the changed file to be flagged, got: {problems}"
    print(f"\n--resend state-validation test -> content drift correctly caught: {problems}")
    shutil.rmtree(state_dir)

    # ---- 10. A chunk-total mismatch against an in-progress transfer must
    #          be surfaced to the user (reset_note), not just silently wipe
    #          their already-collected chunks -- that silence is exactly
    #          what made a legitimate reset look like a "combining broke"
    #          bug from the user's side. -----------------------------------
    cb.reset_progress()
    first_chunks = {1: "aaa", 2: "bbb"}  # total=5, 3 still missing
    cb.scan_video_sync = lambda video_path, status, loop, baseline=frozenset(): (first_chunks, 5, None)
    try:
        await cb.process_video(dummy_path, status)
        raise SystemExit("FAIL: expected IncompleteTransfer for the first (5-total) upload")
    except cb.IncompleteTransfer as e:
        assert e.reset_note is None, "the first upload of a transfer should not report a reset"
    assert cb.accumulated_total == 5 and len(cb.accumulated_chunks) == 2

    second_chunks = {1: "ccc"}  # total=9 now -- disagrees with the 5 already in progress
    cb.scan_video_sync = lambda video_path, status, loop, baseline=frozenset(): (second_chunks, 9, None)
    try:
        await cb.process_video(dummy_path, status)
        raise SystemExit("FAIL: expected IncompleteTransfer for the mismatched-total upload")
    except cb.IncompleteTransfer as e:
        assert e.reset_note is not None, "a total mismatch must surface a reset_note to the user"
        assert "2 chunk(s)" in e.reset_note, f"reset_note should mention the discarded count: {e.reset_note!r}"
    assert cb.accumulated_total == 9 and set(cb.accumulated_chunks) == {1}, \
        "old chunks from the mismatched transfer must not linger after the reset"
    cb.reset_progress()
    print("Total-mismatch reset -> correctly surfaced to the user via reset_note")

    # ---- 11. Regression: a chunk payload starting or ending with a literal
    #          space must round-trip exactly. Space is one of base45's 45
    #          alphabet characters -- legitimate payload content, not
    #          incidental whitespace -- so a chunk boundary can genuinely
    #          land on one. scan_video_sync/scan_image_sync used to
    #          .strip() the raw decoded QR text, silently eating that
    #          character whenever it did. That's deterministic (a property
    #          of the chunk's content and chunk-size boundary, not of
    #          capture quality), which is exactly what made one specific
    #          chunk fail its own checksum every single time, regardless
    #          of re-recording, chunk-size, or framerate. -----------------
    space_img_dir = Path(tempfile.mkdtemp(prefix="qrtest_space_"))
    space_payload = " " + "A" * 30 + " "  # leading AND trailing space
    space_cs = hashlib.sha256(space_payload.encode('ascii')).hexdigest()[:8]
    space_frame = f"7/50/{space_cs}:{space_payload}"

    space_img_path = space_img_dir / "frame.png"
    cv2.imwrite(str(space_img_path), render_qr_frame_image([space_frame]))

    space_chunks, space_total, _ = cb.scan_image_sync(space_img_path)
    assert space_chunks.get(7) == space_payload, (
        f"a leading/trailing space in the payload must survive the decode "
        f"round trip: got {space_chunks.get(7)!r}, expected {space_payload!r}"
    )
    print("Leading/trailing-space payload test -> round-tripped exactly, no corruption")
    shutil.rmtree(space_img_dir)

    shutil.rmtree(src)
    shutil.rmtree(extract_dir)
    shutil.rmtree(img_dir)
    print("\nALL TESTS PASSED")


asyncio.run(main())
