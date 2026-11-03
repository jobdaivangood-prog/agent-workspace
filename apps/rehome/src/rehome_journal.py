"""Bounded, read-only plain/Zstandard journal bytes; no plaintext disk staging.

DB paths may name .jsonl or .jsonl.zst. Both address the same logical
.jsonl stream; exactly one physical representation must exist.
Offsets always address decompressed bytes. Ordinal/JSON/newline validation is
the caller's responsibility. Prefix mode exposes no plaintext beyond its
cutoff to Python/the parser; native block decoding and compressed read-ahead
can process more. Prefix mode does not validate unread tail/frame checksums.
Full mode validates all frames and their EOF.

Stable C API verified against upstream lib/zstd.h, tag v1.5.7:
https://github.com/facebook/zstd/blob/v1.5.7/lib/zstd.h
Deployment must provide an explicitly selected, hash-pinned native decoder.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 256 * 1024 * 1024
MAX_LINE_READ = 8 * 1024 * 1024 + 1
CHUNK = 64 * 1024


class JournalError(ValueError):
    pass


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _guard(path):
    """Reject links, junctions, every other Windows reparse point and aliases."""
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise JournalError('Journal links and reparse points are prohibited')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise JournalError('A journal ancestor is not a directory')
    return path


def _logical_path(root, value):
    if isinstance(value, str) and re.match(r'^\\\\\?\\[A-Za-z]:\\', value):
        value = value[4:]
    if not isinstance(value, (str, os.PathLike)):
        raise JournalError('Invalid journal path')
    root, path = Path(root), Path(value)
    if (not root.is_absolute() or not path.is_absolute() or
            str(root).startswith(('\\\\', '//')) or str(path).startswith(('\\\\', '//')) or
            not path.is_relative_to(root) or '..' in path.parts or '..' in root.parts):
        raise JournalError('Journal is outside the selected local profile')
    parts = path.relative_to(root).parts
    if (len(parts) < 2 or parts[0] not in ('sessions', 'archived_sessions') or
            not (path.name.endswith('.jsonl') or path.name.endswith('.jsonl.zst')) or
            any(':' in p or p.endswith((' ', '.')) for p in parts)):
        raise JournalError('Unsupported journal location')
    _guard(root)
    # Native archive compaction can persist the physical compressed filename in
    # rollout_path. Normalize only the exact supported suffix, after validating
    # the original path. resolve_journal still rejects duplicate representations,
    # reparse points, aliases and missing files for either spelling.
    return path.with_suffix('') if path.name.endswith('.jsonl.zst') else path


def resolve_journal(root, value):
    """Return (logical .jsonl path, physical path, compressed flag)."""
    logical = _logical_path(root, value)
    candidates = (logical, Path(str(logical) + '.zst'))
    existing = []
    for candidate in candidates:
        _guard(candidate)
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise JournalError('Journal must be a regular file without hard links')
        existing.append(candidate)
    if len(existing) != 1:
        raise JournalError('Ambiguous journal representations' if existing else 'Journal is missing')
    return logical, existing[0], existing[0] != logical


class _Buffer(ctypes.Structure):
    _fields_ = [('data', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]


class ZstdDecoder:
    """Explicit native library + mandatory SHA256; never searches PATH."""
    def __init__(self, library_path, expected_sha256):
        path = Path(library_path)
        if not path.is_absolute() or '..' in path.parts or str(path).startswith(('\\\\', '//')):
            raise JournalError('Decoder library must have an explicit local absolute path')
        if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
            raise JournalError('A lowercase SHA256 pin is required for the decoder')
        _guard(path)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024 * 1024:
            raise JournalError('Invalid decoder library')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise JournalError('Decoder SHA256 differs')
        try:
            # Windows dependencies may resolve only beside this explicit DLL or
            # in System32, never the CWD/PATH. Non-Windows accepts explicit SOs.
            self.lib = ctypes.CDLL(str(path), **({'winmode': 0x100 | 0x800} if os.name == 'nt' else {}))
            signatures = {
                'ZSTD_versionString': (ctypes.c_char_p, []),
                'ZSTD_createDStream': (ctypes.c_void_p, []),
                'ZSTD_freeDStream': (ctypes.c_size_t, [ctypes.c_void_p]),
                'ZSTD_initDStream': (ctypes.c_size_t, [ctypes.c_void_p]),
                'ZSTD_DCtx_setParameter': (ctypes.c_size_t, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
                'ZSTD_decompressStream': (ctypes.c_size_t, [ctypes.c_void_p, ctypes.POINTER(_Buffer), ctypes.POINTER(_Buffer)]),
                'ZSTD_isError': (ctypes.c_uint, [ctypes.c_size_t]),
            }
            for name, (result, arguments) in signatures.items():
                function = getattr(self.lib, name)
                function.restype, function.argtypes = result, arguments
            self.version = self.lib.ZSTD_versionString().decode('ascii')
        except (OSError, AttributeError, UnicodeError):
            raise JournalError('Pinned decoder could not be loaded') from None
        if _identity(before) != _identity(path.stat()):
            raise JournalError('Decoder library changed while loading')
        self.sha256 = expected_sha256

    def checked(self, value):
        if self.lib.ZSTD_isError(value):
            # Never return decoder payloads or environment details in errors.
            raise JournalError('Invalid or unsupported Zstandard stream')
        return value


class _Decoded:
    def __init__(self, file, decoder, max_window_log):
        self.file, self.decoder = file, decoder
        self.context = decoder.lib.ZSTD_createDStream()
        if not self.context:
            raise JournalError('Cannot allocate Zstandard stream')
        try:
            decoder.checked(decoder.lib.ZSTD_initDStream(self.context))
            # Stable ZSTD_d_windowLogMax enum=100, power-of-two byte bound.
            decoder.checked(decoder.lib.ZSTD_DCtx_setParameter(self.context, 100, max_window_log))
        except BaseException:
            self.close()
            raise
        self.buffer = None
        self.input = _Buffer(None, 0, 0)
        self.last, self.frames, self.physical_eof = 1, 0, False

    def close(self):
        if self.context:
            self.decoder.lib.ZSTD_freeDStream(self.context)
            self.context = None

    def read(self, limit):
        output_storage = ctypes.create_string_buffer(limit)
        while True:
            if self.input.pos == self.input.size and not self.physical_eof:
                data = self.file.read(CHUNK)
                self.physical_eof = not data
                self.buffer = ctypes.create_string_buffer(data)
                self.input = _Buffer(ctypes.cast(self.buffer, ctypes.c_void_p), len(data), 0)
            if self.physical_eof and self.input.pos == self.input.size and self.last == 0:
                return b''
            output = _Buffer(ctypes.cast(output_storage, ctypes.c_void_p), limit, 0)
            before = self.input.pos
            self.last = self.decoder.checked(self.decoder.lib.ZSTD_decompressStream(self.context, ctypes.byref(output), ctypes.byref(self.input)))
            if self.last == 0:
                self.frames += 1
                if self.frames > 4096:
                    raise JournalError('Too many Zstandard frames')
            if output.pos:
                return output_storage.raw[:output.pos]
            if self.physical_eof and self.input.pos == self.input.size:
                if self.last != 0:
                    raise JournalError('Truncated Zstandard frame')
                return b''
            if self.input.pos == before:
                raise JournalError('Zstandard decoder made no progress')


class _BoundedFile:
    def __init__(self, file, size):
        self.file, self.size, self.read_bytes = file, size, 0

    def read(self, size):
        data = self.file.read(min(size, self.size - self.read_bytes + 1))
        self.read_bytes += len(data)
        if self.read_bytes > self.size:
            raise JournalError('Physical journal grew while reading')
        return data


class _Journal:
    def __init__(self, file, compressed, decoder, byte_limit, expected_size, max_bytes, max_window_log):
        self.file, self.compressed = file, compressed
        self.byte_limit, self.expected_size, self.max_bytes = byte_limit, expected_size, max_bytes
        self.decoded_bytes = self.bytes_read = 0
        self.buffer, self.eof, self.evidence, self.failed = bytearray(), False, None, False
        self.backend = _Decoded(file, decoder, max_window_log) if compressed else file

    def _fill(self):
        if self.eof:
            return
        if self.byte_limit is not None and self.decoded_bytes == self.byte_limit:
            self.eof = True
            return
        ceiling = self.byte_limit if self.byte_limit is not None else min(self.max_bytes, self.expected_size if self.expected_size is not None else self.max_bytes)
        # Full reads probe one byte beyond the limit to reject expansion/size
        # mismatches. Prefix output buffers stop exactly at the cutoff; zstd
        # may internally decode a complete block and input may read ahead.
        allowance = ceiling - self.decoded_bytes + (self.byte_limit is None)
        data = self.backend.read(min(CHUNK, allowance))
        self.decoded_bytes += len(data)
        if self.decoded_bytes > ceiling:
            raise JournalError('Decompressed journal exceeds the permitted size')
        if not data:
            self.eof = True
            if self.byte_limit is not None and self.decoded_bytes != self.byte_limit:
                raise JournalError('Journal ends before the prefix cutoff')
            if self.expected_size is not None and self.decoded_bytes != self.expected_size:
                raise JournalError('Decompressed size differs from projection')
        self.buffer.extend(data)

    def readline(self, size=MAX_LINE_READ):
        if self.failed:
            raise JournalError('Journal reader failed and cannot be reused')
        try:
            return self._readline(size)
        except BaseException:
            # Native zstd state must never be reused after a decoder error.
            self.failed = True
            raise

    def _readline(self, size):
        if type(size) is not int or not 1 <= size <= MAX_LINE_READ:
            raise JournalError('A bounded journal line read is required')
        while True:
            newline = self.buffer.find(b'\n', 0, size)
            if newline >= 0 or len(self.buffer) >= size or self.eof:
                count = newline + 1 if newline >= 0 else min(size, len(self.buffer))
                result = bytes(self.buffer[:count])
                del self.buffer[:count]
                self.bytes_read += len(result)
                return result
            self._fill()

    def finish(self):
        if self.failed:
            raise JournalError('Journal reader failed and cannot be verified')
        if self.buffer:
            raise JournalError('Journal scope was not completely consumed')
        if self.byte_limit is not None:
            if self.bytes_read != self.byte_limit:
                raise JournalError('Journal prefix was not completely consumed')
        elif not self.eof:
            # Validate the actual end of all frames even when the caller stops
            # exactly at the projection offset without one final readline().
            self._fill()
            if self.buffer or not self.eof:
                raise JournalError('Journal scope was not completely consumed')

    def close(self):
        if self.compressed:
            self.backend.close()


@contextmanager
def open_journal(root, rollout_path, byte_limit=None, *, expected_size=None,
                 decoder=None, max_bytes=MAX_BYTES, max_physical_bytes=MAX_BYTES,
                 max_window_log=28):
    """Yield bounded readline stream. `evidence` is set only after valid exit.

    byte_limit: exact prefix output length; no unread tail validation.
    expected_size: exact full decompressed length, checked through EOF.
    These modes are mutually exclusive. Limits can only lower hard ceilings.
    """
    for value in (max_bytes, max_physical_bytes):
        if type(value) is not int or not 1 <= value <= MAX_BYTES:
            raise JournalError('Invalid journal size limit')
    if type(max_window_log) is not int or not 10 <= max_window_log <= 28:
        raise JournalError('Invalid decoder window limit')
    if byte_limit is not None and expected_size is not None:
        raise JournalError('Choose prefix or full-size validation')
    for value in (byte_limit, expected_size):
        if value is not None and (type(value) is not int or not 0 < value <= max_bytes):
            raise JournalError('Invalid decompressed boundary')
    logical, physical, compressed = resolve_journal(root, rollout_path)
    before = physical.lstat()
    if before.st_size > max_physical_bytes:
        raise JournalError('Physical journal exceeds the permitted size')
    if compressed and not isinstance(decoder, ZstdDecoder):
        raise JournalError('An explicitly pinned Zstandard decoder is required')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(physical, flags), 'rb') as file:
        opened = os.fstat(file.fileno())
        if _identity(opened) != _identity(before):
            raise JournalError('Journal changed while opening')
        bounded_file = _BoundedFile(file, before.st_size)
        stream = _Journal(bounded_file, compressed, decoder, byte_limit, expected_size, max_bytes, max_window_log)
        stream.physical_size = before.st_size
        try:
            yield stream
            stream.finish()
            if (resolve_journal(root, logical) != (logical, physical, compressed) or
                    _identity(opened) != _identity(os.fstat(file.fileno())) or
                    _identity(before) != _identity(physical.lstat())):
                raise JournalError('Journal changed while reading')
            stream.evidence = {'representation': 'zstd' if compressed else 'plain',
                'physical_size': before.st_size, 'physical_identity': list(_identity(before)),
                'physical_bytes_read': bounded_file.read_bytes,
                'decoded_bytes': stream.decoded_bytes, 'bytes_read': stream.bytes_read,
                'prefix_only': byte_limit is not None,
                'full_frame_validation': compressed and byte_limit is None,
                'decoder_version': decoder.version if compressed else None,
                'decoder_sha256': decoder.sha256 if compressed else None}
        finally:
            stream.close()


def read_header(root, rollout_path, *, decoder=None, max_header_bytes=8*1024*1024,
                max_physical_bytes=MAX_BYTES):
    """Read exactly the first newline-bounded record, without claiming tail validity.

    Used for provenance diagnostics, never to approve a history segment. A zstd
    block/input may read ahead, but only the first bounded line is returned to
    the caller. No decoded staging, frame-EOF promise or implicit decoder.
    """
    if type(max_header_bytes) is not int or not 1 <= max_header_bytes < MAX_LINE_READ:
        raise JournalError('Invalid header limit')
    if type(max_physical_bytes) is not int or not 1 <= max_physical_bytes <= MAX_BYTES:
        raise JournalError('Invalid physical header read limit')
    logical, physical, compressed = resolve_journal(root, rollout_path)
    before = physical.lstat()
    if before.st_size > max_physical_bytes:
        raise JournalError('Physical journal exceeds the permitted size')
    if compressed and not isinstance(decoder, ZstdDecoder):
        raise JournalError('An explicitly pinned Zstandard decoder is required')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(physical, flags), 'rb') as file:
        opened = os.fstat(file.fileno())
        if _identity(opened) != _identity(before): raise JournalError('Journal changed while opening')
        bounded = _BoundedFile(file, before.st_size)
        stream = _Journal(bounded, compressed, decoder, None, None,
                          min(MAX_BYTES, max_header_bytes + CHUNK), 27)
        try:
            data = stream.readline(max_header_bytes+1)
            if not data.endswith(b'\n') or len(data) > max_header_bytes:
                raise JournalError('Header is oversized or lacks a complete newline boundary')
            if (resolve_journal(root, logical) != (logical, physical, compressed) or
                _identity(opened) != _identity(os.fstat(file.fileno())) or _identity(before) != _identity(physical.lstat())):
                raise JournalError('Journal changed while reading header')
            evidence = {'format': 'rehome-journal-header-read/1', 'representation': 'zstd' if compressed else 'plain',
                'physical_identity': list(_identity(before)), 'physical_bytes_read': bounded.read_bytes,
                'header_bytes': len(data), 'header_sha256': hashlib.sha256(data).hexdigest(),
                'full_frame_validation': False, 'decoder_sha256': decoder.sha256 if compressed else None}
            return data, evidence
        finally: stream.close()
