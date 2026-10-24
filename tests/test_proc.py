"""proc.run_group: a timeout stops the whole job tree (no orphaned kernel / LAMMPS), a normal run is unchanged."""
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'wulffhound'))
from proc import run_group  # noqa: E402


class RunGroupTest(unittest.TestCase):
    def test_timeout_kills_grandchild(self):
        pidfile = os.path.join(tempfile.mkdtemp(), 'gc')
        cmd = ['bash', '-c', 'sleep 300 & echo $! > %s; sleep 300' % pidfile]
        t0 = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_group(cmd, timeout=1, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertLess(time.time() - t0, 8.0)
        gc = int(open(pidfile).read())
        time.sleep(0.3)
        alive = os.path.exists('/proc/%d' % gc) and open('/proc/%d/stat' % gc).read().split()[2] != 'Z'
        self.assertFalse(alive, 'the grandchild survived the timeout')

    def test_normal_run(self):
        r = run_group(['bash', '-c', 'echo ok; exit 3'], timeout=10, stdout=subprocess.PIPE)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout.strip(), b'ok')


if __name__ == '__main__':
    unittest.main()
