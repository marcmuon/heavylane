"""Exercise the release checker on a real history containing encoded private text."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleasePrivacy(unittest.TestCase):
    def test_encoded_identifier_is_detected_after_removal_from_the_tree(self):
        for marker in ('auditTag', 'q7'):
            with self.subTest(marker_length=len(marker)):
                self.assert_policy_detects_encoded_text_and_paths(marker)

    def assert_policy_detects_encoded_text_and_paths(self, marker):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / 'release'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            checker = repo / 'scripts/scrub_check.sh'
            checker.parent.mkdir()
            shutil.copy2(ROOT / 'scripts/scrub_check.sh', checker)
            policy = base / 'private-audit.json'
            policy.write_text(json.dumps({'terms': [marker]}))
            payload = repo / 'payload.txt'
            payload.write_text(base64.b64encode(marker.encode()).decode() + '\n')

            def check(*args):
                return subprocess.run([str(checker), '--patterns-file', str(policy), *args],
                                      cwd=repo, text=True, capture_output=True, timeout=5)

            def commit(message):
                subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
                subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Release Test',
                                '-c', 'user.email=release@example.invalid', 'commit', '-qm', message], check=True)

            detected = check()
            self.assertEqual(detected.returncode, 1, detected.stderr)
            self.assertIn('encoded private audit term', detected.stdout)
            self.assertNotIn(marker, detected.stdout)
            commit('First release tree')
            private_name = repo / ('output-' + marker + '.txt')
            payload.rename(private_name)
            private_name.write_text(marker + '\n')
            filename = check()
            self.assertEqual(filename.returncode, 1, filename.stderr)
            self.assertNotIn(marker, filename.stdout)
            commit('Private filename')
            encoded = base64.b64encode(marker.encode()).decode()
            encoded_name = repo / ('output-' + encoded + '.txt')
            private_name.rename(encoded_name)
            encoded_filename = check()
            self.assertEqual(encoded_filename.returncode, 1, encoded_filename.stderr)
            self.assertNotIn(marker, encoded_filename.stdout)
            self.assertNotIn(encoded, encoded_filename.stdout)
            commit('Encoded private filename')
            encoded_name.unlink()
            commit('Remove private file')
            clean = check()
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            history = check('--history')
            self.assertEqual(history.returncode, 1, history.stderr)
            self.assertIn('encoded private audit term', history.stdout)
            self.assertNotIn(marker, history.stdout)
            self.assertNotIn(encoded, history.stdout)


if __name__ == '__main__':
    unittest.main()
