"""Process-state regressions for background job waiting."""

import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from orch import jobs


class JobTests(unittest.TestCase):
    def test_snapshot_excludes_zombies_and_dead_processes(self):
        output = "1 0 Ss init\n12 1 Z [git] <defunct>\n13 1 Z+ worker\n14 1 X gone\n15 1 S sleep 30\n"
        with patch.object(jobs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)):
            self.assertEqual(jobs.snapshot(), {1: (0, "init"), 15: (1, "sleep 30")})

    def test_wait_checks_state_even_without_a_saved_command(self):
        with patch.object(jobs.os, "waitpid", side_effect=ChildProcessError), \
                patch.object(jobs.os, "kill"):
            for output, command, expected in (
                ("Z [git] <defunct>", "", False),
                ("Z [git] <defunct>", "[git] <defunct>", False),
                ("S sleep 30", "", True),
                ("S sleep 30", "sleep 30", True),
                ("S unrelated", "sleep 30", False),
                ("", "sleep 30", False),
            ):
                with self.subTest(output=output, command=command), patch.object(
                    jobs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)
                ):
                    self.assertEqual(jobs.alive(123, command), expected)

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux process states")
    def test_zombie_owned_by_another_parent_is_not_a_running_job(self):
        # Keep the grandchild unreaped until the test ends, without orphaning it under container PID 1.
        code = """
import os, sys
pid = os.fork()
if pid == 0:
    os._exit(0)
print(pid, flush=True)
sys.stdin.readline()
os.waitpid(pid, 0)
"""
        parent = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        try:
            pid = int(parent.stdout.readline())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                       capture_output=True, text=True, check=True).stdout.strip()
                if state.startswith("Z"):
                    break
                time.sleep(0.01)
            self.assertTrue(state.startswith("Z"), state)
            os.kill(pid, 0)  # Existence alone incorrectly reports this job as alive.
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            self.assertFalse(jobs.alive(pid))
            self.assertNotIn(pid, jobs.snapshot())
            self.assertTrue(jobs.alive(parent.pid))
        finally:
            parent.communicate("\n", timeout=5)


if __name__ == "__main__":
    unittest.main()
