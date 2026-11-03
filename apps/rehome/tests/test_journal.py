import ctypes
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rehome_journal as journal


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = Path(os.environ.get('REHOME_TEST_DECODER', str(ROOT / 'output/test-deps/libzstd.dll')))
LIBRARY_SHA256 = '8f07e1112ed283e5cd2798833e9a3c32d8961381bc36da04af57a1b0ca9bd40b'


class JournalTests(unittest.TestCase):
    def test_header_only_is_bounded_and_does_not_require_valid_later_json(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b'{"type":"session_meta"}\nINVALID_LATER_BYTES\xff')
        data, evidence = journal.read_header(self.root, self.path)
        self.assertEqual(data, b'{"type":"session_meta"}\n')
        self.assertFalse(evidence['full_frame_validation'])
        with self.assertRaises(journal.JournalError): journal.read_header(self.root, self.path, max_header_bytes=3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rh-journal-synthetic-')
        self.root = Path(self.temp.name)
        self.path = self.root / 'sessions/2030/01/01/synthetic.jsonl'
        self.path.parent.mkdir(parents=True)
        self.data = b''.join((json.dumps({'ordinal': i, 'text': text}, ensure_ascii=False) + '\n').encode()
                            for i, text in enumerate(['Тестовый вопрос', 'Synthetic answer', 'LATER_PARENT_TEXT']))

    def tearDown(self):
        self.temp.cleanup()

    def read(self, **kwargs):
        with journal.open_journal(self.root, self.path, **kwargs) as stream:
            result = b''
            while line := stream.readline(journal.MAX_LINE_READ):
                result += line
        return result, stream.evidence

    def test_plain_full_size_and_utf8_byte_boundaries(self):
        self.path.write_bytes(self.data)
        data, evidence = self.read(expected_size=len(self.data))
        self.assertEqual(data, self.data)
        self.assertEqual(evidence['representation'], 'plain')
        self.assertEqual(evidence['decoded_bytes'], len(self.data))
        cutoff = self.data.index(b'\n') + 1
        prefix, evidence = self.read(byte_limit=cutoff)
        self.assertEqual(prefix, self.data[:cutoff])
        self.assertEqual(evidence['decoded_bytes'], cutoff)
        self.assertTrue(evidence['prefix_only'])

    def test_plain_sizes_and_early_exit_rejected(self):
        self.path.write_bytes(self.data)
        for kwargs in ({'expected_size': len(self.data) + 1}, {'expected_size': len(self.data) - 1},
                       {'byte_limit': len(self.data) + 1}, {'max_bytes': 5}, {'max_physical_bytes': 5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(journal.JournalError):
                self.read(**kwargs)
        with self.assertRaisesRegex(journal.JournalError, 'consumed'):
            with journal.open_journal(self.root, self.path) as stream:
                stream.readline(1)

    def test_full_exit_checks_eof_without_final_readline(self):
        self.path.write_bytes(b'one\n')
        with journal.open_journal(self.root, self.path, expected_size=4) as stream:
            self.assertEqual(stream.readline(), b'one\n')
        self.assertEqual(stream.evidence['decoded_bytes'], 4)

    def test_ambiguous_missing_and_no_decoder_rejected(self):
        with self.assertRaisesRegex(journal.JournalError, 'missing'):
            self.read()
        Path(str(self.path) + '.zst').write_bytes(b'not valid')
        with self.assertRaisesRegex(journal.JournalError, 'pinned'):
            self.read()
        self.path.write_bytes(self.data)
        with self.assertRaisesRegex(journal.JournalError, 'Ambiguous'):
            self.read()

    def test_explicit_compressed_path_keeps_location_and_ambiguity_guards(self):
        compressed = Path(str(self.path) + '.zst')
        compressed.write_bytes(b'synthetic representation')
        self.assertEqual((self.path, compressed, True), journal.resolve_journal(self.root, compressed))
        self.path.write_bytes(self.data)
        with self.assertRaisesRegex(journal.JournalError, 'Ambiguous'):
            journal.resolve_journal(self.root, compressed)
        for path in (self.root / 'other/archive.jsonl.zst', self.root / 'sessions/../archive.jsonl.zst',
                     self.root / 'sessions/archive.zst', Path('relative.jsonl.zst')):
            with self.subTest(path=path), self.assertRaises(journal.JournalError):
                journal.resolve_journal(self.root, path)
    def test_representations_cannot_change_while_reading(self):
        self.path.write_bytes(self.data)
        with self.assertRaisesRegex(journal.JournalError, 'Ambiguous'):
            with journal.open_journal(self.root, self.path) as stream:
                while stream.readline():
                    pass
                Path(str(self.path) + '.zst').write_bytes(b'synthetic other representation')
        self.assertIsNone(stream.evidence)

    def test_mutation_and_growth_rejected(self):
        self.path.write_bytes(b'one\n')
        with self.assertRaises(journal.JournalError):
            with journal.open_journal(self.root, self.path) as stream:
                self.assertEqual(stream.readline(), b'one\n')
                with self.path.open('ab') as file:
                    file.write(b'new\n')
        self.assertIsNone(stream.evidence)

    def test_path_limits_and_hardlink_rejected(self):
        self.path.write_bytes(self.data)
        for path in (self.root / 'other/x.jsonl', self.root / 'sessions/../x.jsonl',
                     self.root.parent / 'outside.jsonl', self.path.with_suffix('.zst'), Path('relative.jsonl')):
            with self.subTest(path=path), self.assertRaises(journal.JournalError):
                with journal.open_journal(self.root, path):
                    pass
        other = self.root / 'other.jsonl'
        os.link(self.path, other)
        with self.assertRaisesRegex(journal.JournalError, 'hard links'):
            self.read()

    def test_symlink_rejected(self):
        other = self.root / 'other.jsonl'
        other.write_bytes(self.data)
        try:
            self.path.symlink_to(other)
        except OSError:
            self.skipTest('Host does not permit creating synthetic symlinks')
        with self.assertRaisesRegex(journal.JournalError, 'links'):
            self.read()

    def test_reparse_point_rejected(self):
        self.path.write_bytes(self.data)
        original = Path.lstat
        def marked(path):
            value = original(path)
            if path == self.path:
                class Reparse:
                    st_file_attributes = 0x400
                    st_mode = value.st_mode
                return Reparse()
            return value
        with patch.object(Path, 'lstat', marked), self.assertRaisesRegex(journal.JournalError, 'reparse'):
            self.read()

    def test_invalid_limits_and_library_pins(self):
        self.path.write_bytes(self.data)
        for kwargs in ({'max_window_log': 29}, {'max_bytes': journal.MAX_BYTES + 1},
                       {'max_physical_bytes': journal.MAX_BYTES + 1}, {'byte_limit': 0},
                       {'byte_limit': 4, 'expected_size': 4}):
            with self.subTest(kwargs=kwargs), self.assertRaises(journal.JournalError):
                self.read(**kwargs)
        with self.assertRaises(journal.JournalError):
            journal.ZstdDecoder('libzstd.dll', LIBRARY_SHA256)
        if LIBRARY.is_file():
            with self.assertRaisesRegex(journal.JournalError, 'SHA256'):
                journal.ZstdDecoder(LIBRARY, '0' * 64)


@unittest.skipUnless(LIBRARY.is_file(), 'Explicit locally pinned test decoder not available')
class CompressedJournalTests(unittest.TestCase):
    def test_explicit_compressed_database_pointer_reads_same_header_and_full_bytes(self):
        self.compressed(self.compress(self.data))
        compressed = Path(str(self.path) + '.zst')
        header, evidence = journal.read_header(self.root, compressed, decoder=self.decoder)
        self.assertEqual(self.data.splitlines(keepends=True)[0], header)
        with journal.open_journal(self.root, compressed, decoder=self.decoder, expected_size=len(self.data)) as stream:
            data = b''.join(iter(lambda: stream.readline(journal.MAX_LINE_READ), b''))
        self.assertEqual(self.data, data)
        self.assertEqual('zstd', evidence['representation'])

    def test_header_only_compressed_requires_explicit_decoder(self):
        self.compressed(self.compress(self.data))
        with self.assertRaises(journal.JournalError): journal.read_header(self.root, self.path)
        data, evidence = journal.read_header(self.root, self.path, decoder=self.decoder)
        self.assertEqual(data, self.data.splitlines(keepends=True)[0])
        self.assertEqual(evidence['representation'], 'zstd')
        self.assertFalse(evidence['full_frame_validation'])

    setUp = JournalTests.setUp
    tearDown = JournalTests.tearDown
    read = JournalTests.read

    @classmethod
    def setUpClass(cls):
        cls.decoder = journal.ZstdDecoder(LIBRARY, LIBRARY_SHA256)
        lib = cls.decoder.lib
        lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        lib.ZSTD_compressBound.restype = ctypes.c_size_t
        lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        lib.ZSTD_compress.restype = ctypes.c_size_t

    def compress(self, data):
        destination = ctypes.create_string_buffer(self.decoder.lib.ZSTD_compressBound(len(data)))
        source = ctypes.create_string_buffer(data)
        length = self.decoder.checked(self.decoder.lib.ZSTD_compress(destination, len(destination), source, len(data), 1))
        return destination.raw[:length]

    def compressed(self, data):
        Path(str(self.path) + '.zst').write_bytes(data)

    def test_compressed_full_and_exact_prefix(self):
        self.compressed(self.compress(self.data))
        actual, evidence = self.read(decoder=self.decoder, expected_size=len(self.data))
        self.assertEqual(actual, self.data)
        self.assertTrue(evidence['full_frame_validation'])
        self.assertEqual(evidence['decoder_version'], '1.5.7')
        cutoff = self.data.index(b'\n') + 1
        actual, evidence = self.read(decoder=self.decoder, byte_limit=cutoff)
        self.assertEqual(actual, self.data[:cutoff])
        self.assertEqual(evidence['decoded_bytes'], cutoff)
        self.assertFalse(evidence['full_frame_validation'])

    def test_concatenated_and_empty_frames(self):
        self.compressed(self.compress(b'') + self.compress(self.data[:7]) + self.compress(self.data[7:]) + self.compress(b''))
        self.assertEqual(self.read(decoder=self.decoder, expected_size=len(self.data))[0], self.data)

    def test_truncated_corrupt_trailing_and_size_mismatch(self):
        frame = self.compress(self.data)
        for content in (b'', frame[:-1], frame + b'trailing junk', b'invalid', frame + self.compress(b'')[:-1]):
            with self.subTest(length=len(content)):
                self.compressed(content)
                with self.assertRaises(journal.JournalError):
                    self.read(decoder=self.decoder)
        self.compressed(frame)
        for kwargs in ({'expected_size': len(self.data) + 1}, {'expected_size': len(self.data) - 1},
                       {'byte_limit': len(self.data) + 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(journal.JournalError):
                self.read(decoder=self.decoder, **kwargs)

    def test_decompression_bomb_and_window_limit(self):
        self.compressed(self.compress(b'x' * 1000000 + b'\n'))
        with self.assertRaisesRegex(journal.JournalError, 'size'):
            self.read(decoder=self.decoder, max_bytes=100)
        with self.assertRaisesRegex(journal.JournalError, 'Zstandard'):
            self.read(decoder=self.decoder, max_window_log=10)

    def test_prefix_does_not_decode_later_invalid_frame(self):
        prefix = b'SYNTHETIC inherited\n'
        self.compressed(self.compress(prefix) + b'invalid unread tail')
        actual, evidence = self.read(decoder=self.decoder, byte_limit=len(prefix))
        self.assertEqual(actual, prefix)
        self.assertEqual(evidence['decoded_bytes'], len(prefix))
        with self.assertRaises(journal.JournalError):
            self.read(decoder=self.decoder)

    def test_excessive_empty_frames_rejected(self):
        self.compressed(self.compress(b'') * 4097)
        with self.assertRaisesRegex(journal.JournalError, 'frames'):
            self.read(decoder=self.decoder)

    def test_large_output_flushes_after_compressed_input_eof(self):
        data = b'abcde' * 200000 + b'\n'
        self.compressed(self.compress(data))
        self.assertEqual(self.read(decoder=self.decoder, expected_size=len(data))[0], data)

    def test_decoder_error_cannot_be_caught_and_reused(self):
        self.compressed(b'invalid frame')
        with self.assertRaisesRegex(journal.JournalError, 'cannot be verified'):
            with journal.open_journal(self.root, self.path, decoder=self.decoder) as stream:
                with self.assertRaises(journal.JournalError):
                    stream.readline()
                with self.assertRaisesRegex(journal.JournalError, 'cannot be reused'):
                    stream.readline()
        self.assertIsNone(stream.evidence)

    def test_compressed_growth_cannot_run_past_original_size(self):
        self.compressed(self.compress(b'one\n'))
        with self.assertRaises(journal.JournalError):
            with journal.open_journal(self.root, self.path, decoder=self.decoder) as stream:
                self.assertEqual(stream.readline(), b'one\n')
                with Path(str(self.path) + '.zst').open('ab') as file:
                    file.write(self.compress(b'') * 4096)
        self.assertIsNone(stream.evidence)

    def test_present_checksum_is_verified(self):
        lib = self.decoder.lib
        lib.ZSTD_createCCtx.argtypes, lib.ZSTD_createCCtx.restype = [], ctypes.c_void_p
        lib.ZSTD_freeCCtx.argtypes, lib.ZSTD_freeCCtx.restype = [ctypes.c_void_p], ctypes.c_size_t
        lib.ZSTD_CCtx_setParameter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.ZSTD_CCtx_setParameter.restype = ctypes.c_size_t
        lib.ZSTD_compress2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        lib.ZSTD_compress2.restype = ctypes.c_size_t
        ctx = lib.ZSTD_createCCtx()
        self.assertTrue(ctx)
        try:
            # Stable ZSTD_c_checksumFlag=201, checked against v1.5.7 zstd.h.
            self.decoder.checked(lib.ZSTD_CCtx_setParameter(ctx, 201, 1))
            output = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(self.data)))
            source = ctypes.create_string_buffer(self.data)
            length = self.decoder.checked(lib.ZSTD_compress2(ctx, output, len(output), source, len(self.data)))
            frame = output.raw[:length]
        finally:
            lib.ZSTD_freeCCtx(ctx)
        self.compressed(frame)
        self.assertEqual(self.read(decoder=self.decoder)[0], self.data)
        self.compressed(frame[:-1] + bytes([frame[-1] ^ 1]))
        with self.assertRaises(journal.JournalError):
            self.read(decoder=self.decoder)


if __name__ == '__main__':
    unittest.main()
