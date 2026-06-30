"""osc_demo.py drives a console with the app's real encoder so X32-Edit mirrors
the app. Verify its one-shot move set actually reaches a desk over the socket."""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import osc_demo
from trio_mix.emulator import EmulatedDesk
from trio_mix.osc import OscConsole


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class OscDemoDrivesDesk(unittest.TestCase):
    def setUp(self):
        self.desk = EmulatedDesk(listen_port=_free_port(), app_ip="127.0.0.1",
                                 app_port=_free_port())
        self.desk.start()
        time.sleep(0.1)
        self.con = OscConsole("127.0.0.1", port=self.desk.listen_port)

    def tearDown(self):
        self.con.close()
        self.desk.stop()

    def test_one_shot_moves_reach_the_desk(self):
        osc_demo.one_shot(self.con)
        time.sleep(0.25)
        self.assertIn(1, self.desk.fader)              # faders landed in the model
        self.assertTrue(self.desk.mute.get(6))         # ch6 muted (on=0)
        self.assertFalse(self.desk.mute.get(7))        # ch7 on
        # the EQ-notch SETs were stored in the param model the editor reads
        self.assertIn("/ch/08/eq/1/f", self.desk._params)
        self.assertGreater(self.desk.recv_count, 0)

    def test_animate_one_pass_sends_in_range(self):
        # drive one wave frame's worth of moves; nothing should raise / clamp safely
        import math
        for ch in range(1, 9):
            db = -8.0 + 11.0 * 0.5 * (1.0 + math.sin(0.3 + ch * 0.7))
            self.con.set_fader_db(ch, db)
        time.sleep(0.2)
        self.assertGreaterEqual(len(self.desk.fader), 8)


if __name__ == "__main__":
    unittest.main()
