"""Release gates must reject incompatible binaries, even before native inference."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'verify_bundle', Path(__file__).resolve().parents[1] / 'scripts/verify_bundle.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class ReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = Path(temporary.name).resolve() / 'Example.app'
        self.app.mkdir()
        self.binary = self.app / 'native.dylib'
        self.binary.write_bytes(b'\xcf\xfa\xed\xfe' + b'test')

    def inspect(self, arch='arm64', dependency='@rpath/libmlx.dylib', minimum='15.0'):
        def result(arguments, **kwargs):
            if arguments[0].endswith('lipo'):
                return arch
            if arguments[1] == '-L':
                return f'{self.binary}:\n\t{dependency} (compatibility version 1.0.0)\n'
            return f'Load command 1\n cmd LC_BUILD_VERSION\n platform 1\n minos {minimum}\n sdk 26.0\n'
        with patch.object(verify, 'run', side_effect=result):
            return verify.inspect_binaries(self.app)

    def test_accepts_arm64_and_declared_macos_minimum(self):
        self.assertEqual(self.inspect(), ['native.dylib'])

    def test_rejects_intel_only_binary(self):
        with self.assertRaisesRegex(RuntimeError, 'lacks arm64'):
            self.inspect(arch='x86_64')

    def test_rejects_homebrew_library_reference(self):
        with self.assertRaisesRegex(RuntimeError, 'External library dependency'):
            self.inspect(dependency='/opt/homebrew/lib/libmlx.dylib')

    def test_rejects_higher_deployment_target(self):
        with self.assertRaisesRegex(RuntimeError, 'needs macOS 15.1'):
            self.inspect(minimum='15.1')

    def test_rejects_external_symlink(self):
        (self.app / 'external').symlink_to(self.app.parent)
        with self.assertRaisesRegex(RuntimeError, 'External bundle symlink'):
            self.inspect()


if __name__ == '__main__':
    unittest.main()
