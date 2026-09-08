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
import os
import random
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import sender
import converter_bot as cb


class FakeStatus:
    async def update(self, text, force=False):
        pass


def parse_frames_to_scan_result(frames):
    """Mimic what scan_video_sync's inner loop does, using the real regexes."""
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
        idx, total, payload = int(m.group(1)), int(m.group(2)), m.group(3)
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

    cb.scan_video_sync = lambda video_path, status, loop: (v1_chunks, v1_total, v1_hash)

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
    cb.scan_video_sync = lambda video_path, status, loop: (v2_chunks, v2_total, v2_hash)

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

    cb.scan_video_sync = lambda video_path, status, loop: (corrupt_chunks, full_total, digest)
    try:
        await cb.process_video(dummy_path, status)
        raise SystemExit("FAIL: expected checksum ValueError on corrupted chunk, got success")
    except ValueError as e:
        assert "Checksum mismatch" in str(e), f"wrong error: {e}"
        print(f"\nCorruption test -> correctly caught: {e}")

    shutil.rmtree(src)
    shutil.rmtree(extract_dir)
    print("\nALL TESTS PASSED")


asyncio.run(main())
