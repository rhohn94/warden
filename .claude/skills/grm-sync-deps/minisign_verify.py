#!/usr/bin/env python3
"""minisign_verify.py — pure-Python, stdlib-only ed25519 / minisign verifier.

Part of the Dependency Channel artifact-signing feature (v3.79, #318 / FC-2 —
`docs/grimoire/design/dependency-channel-design.md` §Signing). Producers sign
`SHA256SUMS` with the `minisign` CLI (`build_distributables.py.sign_checksums`,
unchanged); this module is the **consumer-side verifier** and deliberately does
NOT shell out to `minisign` — it re-implements the wire format + the Ed25519
verify equation directly (RFC 8032 reference algorithm, stdlib `hashlib`/`base64`
only) so `grm-sync-deps` never grows a non-stdlib runtime dependency and works
identically whether or not the `minisign` binary is installed.

Wire formats implemented (minisign-compatible, "Ed" — non-prehashed — mode only;
the "ED"/blake2b-prehashed mode used for multi-gigabyte files is out of scope,
since `SHA256SUMS` is always small):

  Public key blob (`pubkey` in vendor.toml, or a `minisign.pub` file):
      untrusted comment: <freeform>
      <base64: 2 bytes 'Ed' + 8 bytes key id + 32 bytes public key>

  Detached signature (`SHA256SUMS.minisig`):
      untrusted comment: <freeform>
      <base64: 2 bytes 'Ed' + 8 bytes key id + 64 bytes signature>
      trusted comment: <freeform, signed>
      <base64: 64 bytes — a second Ed25519 signature over
       (the 64-byte signature above || the trusted-comment text)>

A signing helper (`generate_keypair` / `sign`) is included ONLY so `--self-test`
can produce a full, deterministic, offline round-trip fixture without shelling
out to the real `minisign` tool (unavailable in a bare CI/agent sandbox). It is
NOT used by any producer path — `build_distributables.py` keeps using the real
`minisign` CLI for actual releases; this module's signer exists purely to make
verification self-testable.

Public surface:
  MinisignError                  raised on any malformed blob (never silent).
  parse_public_key(text)          -> (key_id: bytes, pubkey: bytes)
  parse_signature(text)           -> dict with keyid/sig/trusted_comment/global_sig
  verify(pubkey_text, sig_text, message: bytes, verify_global=True) -> bool
  generate_keypair(seed: bytes)    -> (sk: bytes, pk: bytes)             [test-only]
  sign(sk, pk, message, key_id, trusted_comment) -> (pubkey_text, sig_text)  [test-only]
  run_self_test()                 -> bool
"""
from __future__ import annotations

import base64
import hashlib

# ── RFC 8032 Ed25519 reference arithmetic (public-domain djb ed25519.py shape) ──
# Pure stdlib (hashlib only); deliberately unoptimized/reference-style — this
# runs once per synced dependency, never in a hot loop.

_b = 256
_q = 2 ** 255 - 19
_l = 2 ** 252 + 27742317777372353535851937790883648493


def _H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _inv(x: int) -> int:
    return pow(x, _q - 2, _q)


_d = -121665 * _inv(121666) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = 4 * _inv(5)
_Bx = _xrecover(_By)
_BASE = (_Bx % _q, _By % _q)


def _edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2)
    return (x3 % _q, y3 % _q)


def _scalarmult(P, e: int):
    if e == 0:
        return (0, 1)
    Q = _scalarmult(P, e // 2)
    Q = _edwards(Q, Q)
    if e & 1:
        Q = _edwards(Q, P)
    return Q


def _encodeint(y: int) -> bytes:
    return y.to_bytes(_b // 8, "little")


def _encodepoint(P) -> bytes:
    x, y = P
    out = bytearray(y.to_bytes(_b // 8, "little"))
    if x & 1:
        out[-1] |= 0x80
    return bytes(out)


def _decodeint(s: bytes) -> int:
    return int.from_bytes(s, "little")


def _isoncurve(P) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _q == 0


def _decodepoint(s: bytes):
    y = int.from_bytes(s, "little") & ((1 << (_b - 1)) - 1)
    sign_bit = (s[-1] >> 7) & 1
    x = _xrecover(y)
    if (x & 1) != sign_bit:
        x = _q - x
    P = (x, y)
    if not _isoncurve(P):
        raise MinisignError("Ed25519 point is not on the curve")
    return P


def _Hint(m: bytes) -> int:
    return int.from_bytes(_H(m), "little")


def _ed25519_publickey(sk: bytes) -> bytes:
    h = _H(sk)
    a = 2 ** (_b - 2) + sum(
        2 ** i * ((h[i // 8] >> (i % 8)) & 1) for i in range(3, _b - 2)
    )
    A = _scalarmult(_BASE, a)
    return _encodepoint(A)


def _ed25519_sign(m: bytes, sk: bytes, pk: bytes) -> bytes:
    h = _H(sk)
    a = 2 ** (_b - 2) + sum(
        2 ** i * ((h[i // 8] >> (i % 8)) & 1) for i in range(3, _b - 2)
    )
    r = _Hint(h[_b // 8 : _b // 4] + m)
    R = _scalarmult(_BASE, r)
    S = (r + _Hint(_encodepoint(R) + pk + m) * a) % _l
    return _encodepoint(R) + _encodeint(S)


def _ed25519_verify(sig: bytes, m: bytes, pk: bytes) -> bool:
    if len(sig) != _b // 4:
        raise MinisignError("signature length is wrong")
    if len(pk) != _b // 8:
        raise MinisignError("public-key length is wrong")
    try:
        R = _decodepoint(sig[: _b // 8])
        A = _decodepoint(pk)
    except MinisignError:
        return False
    S = _decodeint(sig[_b // 8 : _b // 4])
    if S >= _l:
        return False
    h = _Hint(_encodepoint(R) + pk + m)
    x1, y1 = _scalarmult(_BASE, S)
    x2, y2 = _edwards(R, _scalarmult(A, h))
    return x1 == x2 and y1 == y2


# ── Errors ───────────────────────────────────────────────────────────────────

class MinisignError(Exception):
    """A malformed key/signature blob, or a verification precondition failure.

    Never raised for "verification returned false" — that is a normal `bool`
    return from `verify()`. Raised only when the *input itself* cannot be
    parsed (a config/transport problem, distinct from "the signature is bad").
    """


# ── minisign wire-format parsing ─────────────────────────────────────────────

_ALG_ED = b"Ed"  # non-prehashed — the only mode this module supports (small files)


def _b64_payload_line(text: str, min_lines: int = 2):
    """Split a minisign-format text blob into its non-empty lines."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip() != ""]
    if len(lines) < min_lines:
        raise MinisignError(
            f"minisign blob has {len(lines)} non-empty line(s), need >= {min_lines}"
        )
    return lines


def parse_public_key(text: str) -> tuple:
    """Parse a minisign public-key blob. Returns (key_id: bytes, pubkey: bytes)."""
    lines = _b64_payload_line(text, min_lines=1)
    # The public key is the last non-comment line (tolerates a leading
    # "untrusted comment:" line, or a bare base64 blob with no comment at all).
    b64_line = lines[-1]
    try:
        raw = base64.b64decode(b64_line, validate=True)
    except Exception as exc:  # noqa: BLE001 - any base64 failure is a parse error
        raise MinisignError(f"public key is not valid base64: {exc}") from exc
    if len(raw) != 42:
        raise MinisignError(f"public key blob must decode to 42 bytes, got {len(raw)}")
    alg, key_id, pubkey = raw[0:2], raw[2:10], raw[10:42]
    if alg != _ALG_ED:
        raise MinisignError(f"unsupported public-key algorithm {alg!r} (only 'Ed')")
    return key_id, pubkey


def parse_signature(text: str) -> dict:
    """Parse a `.minisig` detached signature. Returns a dict of its fields."""
    lines = [ln for ln in text.strip("\n").split("\n") if ln.strip() != ""]
    if len(lines) < 2:
        raise MinisignError("signature blob has too few lines")
    # Line layout: [untrusted comment], sig_b64, [trusted comment], global_sig_b64.
    # Tolerate an absent trusted-comment pair (global-sig verification is then
    # skipped by the caller) as well as the full 4-line form.
    sig_b64 = lines[1] if lines[0].lower().startswith("untrusted comment:") else lines[0]
    try:
        raw = base64.b64decode(sig_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise MinisignError(f"signature is not valid base64: {exc}") from exc
    if len(raw) != 74:
        raise MinisignError(f"signature blob must decode to 74 bytes, got {len(raw)}")
    alg, key_id, sig = raw[0:2], raw[2:10], raw[10:74]
    if alg != _ALG_ED:
        raise MinisignError(f"unsupported signature algorithm {alg!r} (only 'Ed')")
    result = {
        "key_id": key_id,
        "sig": sig,
        "trusted_comment": None,
        "trusted_comment_raw": None,
        "global_sig": None,
    }
    # Look for the trusted-comment pair after the sig line.
    start = lines.index(sig_b64) if sig_b64 in lines else 1
    rest = lines[start + 1 :]
    for i, ln in enumerate(rest):
        if ln.lower().startswith("trusted comment:"):
            comment = ln.split(":", 1)[1].lstrip() if ":" in ln else ""
            if i + 1 < len(rest):
                try:
                    gsig = base64.b64decode(rest[i + 1], validate=True)
                except Exception as exc:  # noqa: BLE001
                    raise MinisignError(
                        f"global signature is not valid base64: {exc}"
                    ) from exc
                if len(gsig) != 64:
                    raise MinisignError(
                        f"global signature must decode to 64 bytes, got {len(gsig)}"
                    )
                result["trusted_comment"] = comment
                result["trusted_comment_raw"] = comment.encode("utf-8")
                result["global_sig"] = gsig
            break
    return result


def verify(pubkey_text: str, sig_text: str, message: bytes,
           verify_global: bool = True) -> bool:
    """Verify `message` (the raw `SHA256SUMS` bytes) against a pinned pubkey.

    Returns a plain `bool` — never raises for "signature does not match" (that
    is the expected soft-fail outcome the caller records as
    `signature_verified: false`). Raises `MinisignError` only for a malformed
    key/signature blob (a config problem, not a tamper signal).

    When the sidecar carries a trusted-comment + global signature and
    `verify_global` is True, the global signature (over `sig || comment`) is
    also checked — a tampered *comment* alone would otherwise go unnoticed.
    """
    key_id, pubkey = parse_public_key(pubkey_text)
    parsed = parse_signature(sig_text)
    if parsed["key_id"] != key_id:
        # Key-id mismatch is not fatal (the same key can carry an id used only
        # as a lookup hint) — real minisign warns; we mirror that laxness here
        # since `vendor.toml` already pins the pubkey explicitly out-of-band.
        pass
    if not _ed25519_verify(parsed["sig"], message, pubkey):
        return False
    if verify_global and parsed["global_sig"] is not None:
        global_message = parsed["sig"] + parsed["trusted_comment_raw"]
        if not _ed25519_verify(parsed["global_sig"], global_message, pubkey):
            return False
    return True


# ── Test-only signing helpers (NOT used by any producer path) ───────────────

def generate_keypair(seed: bytes) -> tuple:
    """Derive a deterministic Ed25519 keypair from a 32-byte seed. Test-only."""
    if len(seed) != 32:
        raise MinisignError("seed must be exactly 32 bytes")
    pk = _ed25519_publickey(seed)
    return seed, pk


def _format_public_key(key_id: bytes, pk: bytes, comment: str = "test key") -> str:
    blob = _ALG_ED + key_id + pk
    return (
        f"untrusted comment: minisign public key ({comment})\n"
        f"{base64.b64encode(blob).decode()}\n"
    )


def sign(sk: bytes, pk: bytes, message: bytes, key_id: bytes,
         trusted_comment: str = "self-test signature") -> tuple:
    """Produce a minisign-format (pubkey_text, sig_text) pair. Test-only.

    Mirrors the real `minisign -S` output shape closely enough for `verify()`
    to round-trip it, including the trusted-comment global signature.
    """
    sig = _ed25519_sign(message, sk, pk)
    sig_blob = _ALG_ED + key_id + sig
    global_message = sig + trusted_comment.encode("utf-8")
    global_sig = _ed25519_sign(global_message, sk, pk)
    sig_text = (
        "untrusted comment: signature from minisign secret key\n"
        f"{base64.b64encode(sig_blob).decode()}\n"
        f"trusted comment: {trusted_comment}\n"
        f"{base64.b64encode(global_sig).decode()}\n"
    )
    pubkey_text = _format_public_key(key_id, pk)
    return pubkey_text, sig_text


# ── Self-test (deterministic, offline, stdlib-only) ─────────────────────────

def run_self_test() -> bool:
    failures = []

    def check(cond, label):
        if cond:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}")
            failures.append(label)

    seed = hashlib.sha256(b"grimoire-minisign-self-test-seed").digest()
    sk, pk = generate_keypair(seed)
    key_id = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    message = b"deadbeef  widget-v1.0.0.tar.gz\n"
    pubkey_text, sig_text = sign(sk, pk, message, key_id)

    # 1) A correctly signed message verifies.
    check(verify(pubkey_text, sig_text, message) is True,
          "valid signature verifies true")

    # 2) A tampered message fails verification (no exception — a plain False).
    check(verify(pubkey_text, sig_text, message + b"tampered") is False,
          "tampered message verifies false")

    # 3) A signature checked against the wrong public key fails.
    seed2 = hashlib.sha256(b"a different seed").digest()
    _sk2, pk2 = generate_keypair(seed2)
    other_pubkey_text = _format_public_key(key_id, pk2)
    check(verify(other_pubkey_text, sig_text, message) is False,
          "signature under wrong pubkey verifies false")

    # 4) A tampered trusted comment (global signature) is caught.
    tampered_sig_text = sig_text.replace(
        "trusted comment: self-test signature",
        "trusted comment: TAMPERED COMMENT",
    )
    check(verify(pubkey_text, tampered_sig_text, message) is False,
          "tampered trusted-comment caught by global signature")

    # 5) Malformed blobs raise MinisignError (never silently accepted).
    bad_pubkey = "untrusted comment: x\nbm90LWEta2V5\n"  # decodes, wrong length
    raised = False
    try:
        verify(bad_pubkey, sig_text, message)
    except MinisignError:
        raised = True
    check(raised, "malformed public key raises MinisignError")

    raised2 = False
    try:
        verify(pubkey_text, "untrusted comment: x\nbm90LWEtc2ln\n", message)
    except MinisignError:
        raised2 = True
    check(raised2, "malformed signature raises MinisignError")

    # 6) A signature without a trusted-comment pair still verifies the core sig.
    bare_sig_text = (
        "untrusted comment: signature from minisign secret key\n"
        f"{base64.b64encode(_ALG_ED + key_id + _ed25519_sign(message, sk, pk)).decode()}\n"
    )
    check(verify(pubkey_text, bare_sig_text, message) is True,
          "signature without trusted-comment pair still verifies")

    print(f"\n{len(failures)} failure(s)." if failures else "\nall checks passed.")
    return not failures


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_self_test() else 1)
