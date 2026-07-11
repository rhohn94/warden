#!/usr/bin/env python3
"""smoke_visual_diff.py — pure-stdlib PNG pixel-diff engine (Grimoire #289, RSS-9).

Reference decoder/encoder + diff logic for the `smoke-visual` recipe
(`scripts/smoke-visual.sh`), the generalized fleet deploy-guardrail harness
(scripted-screenshot smoke, extending the curl-based `smoke` floor —
`docs/grimoire/design/runtime-verification-design.md`).

Deliberately dependency-free (no Pillow, no ImageMagick): headless-browser
screenshots (Playwright/Puppeteer/`chromium --headless --screenshot`) are
virtually always 8-bit, non-interlaced PNGs, so a small stdlib decoder covering
color types 0 (grayscale), 2 (RGB), 4 (grayscale+alpha), 6 (RGBA) at bit depth 8
is sufficient. A baseline/current pair outside that shape is a loud, named
error — never a silent wrong-answer diff.

Usage:
    smoke_visual_diff.py diff <baseline.png> <current.png> <diff_out.png> \
        [--threshold PCT] [--fuzz N]
        # prints a JSON report {pixels_total, pixels_diff, pct, threshold,
        # verdict}; exit 0 = PASS (pct <= threshold), exit 1 = FAIL.
        # diff_out.png is written whenever pixels_diff > 0 (a visual, even a
        # passing one under threshold, is cheap and helps eyeball noise).

    smoke_visual_diff.py make-fixture <out.png> <variant> [--width N] [--height N]
        # variant one of: base | same | tiny-drift | big-drift — offline
        # fixture generator used by smoke-visual.sh --self-test (no browser).

    smoke_visual_diff.py --self-test
"""
import struct
import sys
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"

# color type -> channel count (bit depth 8 only; this decoder does not support
# palette (3), 16-bit, or interlaced PNGs — real screenshot tools never emit
# those, so an unsupported input is a loud, named error).
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


class PngError(Exception):
    pass


# ── decode ───────────────────────────────────────────────────────────────────

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png(path):
    """Return (width, height, channels, rows) — rows is a list of `height`
    bytearrays, each `width * channels` bytes of unfiltered 8-bit samples."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != PNG_SIG:
        raise PngError("%s: not a PNG (bad signature)" % path)
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            (width, height, bit_depth, color_type, _comp, _filt,
             interlace) = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
    if width is None:
        raise PngError("%s: no IHDR chunk" % path)
    if bit_depth != 8:
        raise PngError("%s: unsupported bit depth %d (only 8-bit supported)"
                       % (path, bit_depth))
    if interlace:
        raise PngError("%s: interlaced PNGs are not supported" % path)
    if color_type not in _CHANNELS:
        raise PngError("%s: unsupported color type %d" % (path, color_type))
    channels = _CHANNELS[color_type]
    raw = zlib.decompress(bytes(idat))
    bpp = channels  # 8-bit samples: 1 byte per channel
    stride = width * channels
    rows = []
    prev = bytearray(stride)
    off = 0
    for _ in range(height):
        ftype = raw[off]
        off += 1
        line = bytearray(raw[off:off + stride])
        off += stride
        if ftype == 0:
            pass
        elif ftype == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
        else:
            raise PngError("%s: unsupported filter type %d" % (path, ftype))
        rows.append(line)
        prev = line
    return width, height, channels, rows


# ── encode (filter-type-0 / None; simplest correct encoder) ─────────────────

def write_png(path, width, height, channels, rows):
    color_type = {1: 0, 2: 4, 3: 2, 4: 6}.get(channels)
    if color_type is None:
        raise PngError("write_png: unsupported channel count %d" % channels)
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0 (None)
        raw.extend(row)
    idat = zlib.compress(bytes(raw), 9)

    def chunk(ctype, payload):
        out = struct.pack(">I", len(payload)) + ctype + payload
        out += struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
        return out

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(PNG_SIG)
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", idat))
        fh.write(chunk(b"IEND", b""))


# ── fixtures (offline self-test / CI use, no browser) ────────────────────────

def make_fixture(width, height, variant):
    """Return (width, height, channels=4, rows) for a synthetic RGBA fixture.

    variant:
      base       — solid mid-gray field.
      same       — identical to base (byte-for-byte).
      tiny-drift — base with a single pixel changed (well under a 0.5% floor).
      big-drift  — base with a large block changed (clearly over threshold).
    """
    bg = (100, 100, 100, 255)
    rows = []
    for _y in range(height):
        row = bytearray()
        for _x in range(width):
            row.extend(bg)
        rows.append(row)
    if variant in ("base", "same"):
        pass
    elif variant == "tiny-drift":
        rows[0][0:4] = bytes((200, 50, 50, 255))
    elif variant == "big-drift":
        for y in range(height // 2):
            for x in range(width // 2):
                off = x * 4
                rows[y][off:off + 4] = bytes((200, 50, 50, 255))
    else:
        raise PngError("unknown fixture variant %r" % variant)
    return width, height, 4, rows


# ── diff ─────────────────────────────────────────────────────────────────────

def diff_images(baseline_path, current_path, diff_out_path, threshold_pct, fuzz=32):
    bw, bh, bc, brows = read_png(baseline_path)
    cw, ch, cc, crows = read_png(current_path)
    if (bw, bh) != (cw, ch):
        raise PngError(
            "%s is %dx%d but %s is %dx%d — captures must match the baseline's "
            "viewport exactly" % (current_path, cw, ch, baseline_path, bw, bh))
    channels = max(bc, cc)

    def sample(rows, ch_count, y, x, ch):
        row = rows[y]
        idx = x * ch_count + ch
        if ch >= ch_count:
            return 255 if ch == 3 else 0  # missing alpha = opaque; missing color = 0
        return row[idx]

    total = bw * bh
    diff_count = 0
    diff_rows = [bytearray(bw * channels) for _ in range(bh)]
    for y in range(bh):
        for x in range(bw):
            differs = False
            for ch in range(min(channels, 3)):  # compare color channels only
                bv = sample(brows, bc, y, x, ch)
                cv = sample(crows, cc, y, x, ch)
                if abs(bv - cv) > fuzz:
                    differs = True
                    break
            base_pixel = [sample(brows, bc, y, x, ch) for ch in range(channels)]
            if differs:
                diff_count += 1
                out_pixel = [255, 0, 0, 255][:channels]
            else:
                out_pixel = base_pixel
            off = x * channels
            diff_rows[y][off:off + channels] = bytes(out_pixel)

    pct = (100.0 * diff_count / total) if total else 0.0
    verdict = "PASS" if pct <= threshold_pct else "FAIL"
    if diff_count > 0:
        write_png(diff_out_path, bw, bh, channels, diff_rows)
    return {
        "pixels_total": total,
        "pixels_diff": diff_count,
        "pct": round(pct, 4),
        "threshold": threshold_pct,
        "verdict": verdict,
        "diff_image": diff_out_path if diff_count > 0 else None,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_flag(args, name, default=None, cast=str):
    if name in args:
        i = args.index(name)
        val = args[i + 1]
        del args[i:i + 2]
        return cast(val)
    return default


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: smoke_visual_diff.py diff|make-fixture|--self-test ...",
              file=sys.stderr)
        return 2

    if argv[0] == "--self-test":
        return _self_test()

    if argv[0] == "diff":
        rest = argv[1:]
        threshold = _parse_flag(rest, "--threshold", 0.5, float)
        fuzz = _parse_flag(rest, "--fuzz", 32, int)
        if len(rest) != 3:
            print("usage: smoke_visual_diff.py diff <baseline> <current> <diff_out> "
                  "[--threshold PCT] [--fuzz N]", file=sys.stderr)
            return 2
        baseline, current, diff_out = rest
        try:
            report = diff_images(baseline, current, diff_out, threshold, fuzz)
        except (PngError, FileNotFoundError, zlib.error) as e:
            print("smoke_visual_diff: %s" % e, file=sys.stderr)
            return 2
        import json
        print(json.dumps(report))
        return 0 if report["verdict"] == "PASS" else 1

    if argv[0] == "make-fixture":
        rest = argv[1:]
        width = _parse_flag(rest, "--width", 20, int)
        height = _parse_flag(rest, "--height", 20, int)
        if len(rest) != 2:
            print("usage: smoke_visual_diff.py make-fixture <out.png> <variant> "
                  "[--width N] [--height N]", file=sys.stderr)
            return 2
        out_path, variant = rest
        try:
            w, h, ch, rows = make_fixture(width, height, variant)
            write_png(out_path, w, h, ch, rows)
        except PngError as e:
            print("smoke_visual_diff: %s" % e, file=sys.stderr)
            return 2
        return 0

    print("smoke_visual_diff: unknown subcommand %r" % argv[0], file=sys.stderr)
    return 2


def _self_test():
    import os
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory() as d:
        base = os.path.join(d, "base.png")
        same = os.path.join(d, "same.png")
        tiny = os.path.join(d, "tiny.png")
        big = os.path.join(d, "big.png")
        diff_out = os.path.join(d, "diff.png")

        w, h, ch, rows = make_fixture(20, 20, "base")
        write_png(base, w, h, ch, rows)
        w, h, ch, rows = make_fixture(20, 20, "same")
        write_png(same, w, h, ch, rows)
        w, h, ch, rows = make_fixture(20, 20, "tiny-drift")
        write_png(tiny, w, h, ch, rows)
        w, h, ch, rows = make_fixture(20, 20, "big-drift")
        write_png(big, w, h, ch, rows)

        # round-trip: decode what we just encoded and confirm pixel equality.
        rw, rh, rc, rrows = read_png(base)
        _, _, _, base_rows = make_fixture(20, 20, "base")
        if (rw, rh, rc) != (20, 20, 4) or rrows != base_rows:
            failures.append("decode(encode(base)) != base (round-trip mismatch)")

        # identical images: PASS, no diff artifact written.
        report = diff_images(base, same, diff_out, threshold_pct=0.5, fuzz=32)
        if report["verdict"] != "PASS" or report["pixels_diff"] != 0:
            failures.append("identical images should PASS with 0 diff pixels: %r" % report)
        if os.path.exists(diff_out):
            failures.append("no diff artifact should be written for a 0-diff comparison")

        # tiny drift (1/400 = 0.25%): PASS under a 0.5% threshold.
        report = diff_images(base, tiny, diff_out, threshold_pct=0.5, fuzz=32)
        if report["verdict"] != "PASS":
            failures.append("tiny drift (0.25%%) should PASS a 0.5%% threshold: %r" % report)
        if report["pixels_diff"] != 1:
            failures.append("tiny-drift fixture should differ in exactly 1 pixel: %r" % report)
        if not os.path.exists(diff_out):
            failures.append("a diff artifact should be written even for a passing "
                            "nonzero diff (eyeball-able noise)")
        os.remove(diff_out)

        # big drift (100/400 = 25%): FAIL, diff artifact written.
        report = diff_images(base, big, diff_out, threshold_pct=0.5, fuzz=32)
        if report["verdict"] != "FAIL":
            failures.append("big drift (25%%) should FAIL a 0.5%% threshold: %r" % report)
        if not os.path.exists(diff_out):
            failures.append("FAIL comparison must write a diff artifact")

        # dimension mismatch is a loud, named error (never a silent wrong answer).
        w2, h2, ch2, rows2 = make_fixture(10, 10, "base")
        mismatched = os.path.join(d, "mismatched.png")
        write_png(mismatched, w2, h2, ch2, rows2)
        try:
            diff_images(base, mismatched, diff_out, threshold_pct=0.5, fuzz=32)
            failures.append("dimension mismatch should raise PngError")
        except PngError:
            pass

        # CLI: diff exit code mirrors the verdict.
        rc = main(["diff", base, same, diff_out, "--threshold", "0.5"])
        if rc != 0:
            failures.append("CLI diff of identical images should exit 0")
        rc = main(["diff", base, big, diff_out, "--threshold", "0.5"])
        if rc != 1:
            failures.append("CLI diff of a big drift should exit 1")

        # CLI: make-fixture round-trips through the CLI too.
        cli_fixture = os.path.join(d, "cli-fixture.png")
        rc = main(["make-fixture", cli_fixture, "same", "--width", "8", "--height", "8"])
        if rc != 0 or not os.path.exists(cli_fixture):
            failures.append("CLI make-fixture should exit 0 and write the file")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("smoke_visual_diff self-test: OK (PNG round-trip, identical/tiny/big-drift "
          "diff verdicts, dimension-mismatch loud error, diff-artifact policy, CLI "
          "exit codes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
