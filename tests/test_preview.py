import numpy as np

from stormwatch.preview import SharedPreview


def test_seqlock_roundtrip_and_no_repeat():
    w = SharedPreview.create()
    try:
        r = SharedPreview.attach(w.name)
        seq, img, _ = r.read(0)
        assert img is None
        frame = (np.arange(48 * 64 * 3) % 251).astype(np.uint8).reshape(48, 64, 3)
        w.write(frame, flags=1)
        seq, img, flags = r.read(0)
        assert img is not None and (img == frame).all() and flags == 1
        seq2, img2, _ = r.read(seq)
        assert img2 is None and seq2 == seq
        w.write(frame[:, :, 0])
        seq3, img3, _ = r.read(seq)
        assert img3.shape == (48, 64) and seq3 > seq
        r.close()
    finally:
        w.close()


def test_reader_never_returns_a_half_written_frame():
    w = SharedPreview.create()
    try:
        r = SharedPreview.attach(w.name)
        # Simulate the writer being mid-write (odd sequence).
        import struct

        struct.pack_into("<Q", w.shm.buf, 0, 7)
        assert r.read(0)[1] is None
        r.close()
    finally:
        w.close()
