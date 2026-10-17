# leaktest/controllers/main.py
import os
from odoo import http

LEAK = []  # lives as long as the worker process does

class LeakTest(http.Controller):
    @http.route('/leaktest', type='http', auth='user')
    def leak(self):
        LEAK.append('A' * 100_000_000)  # ~100 MB per visit
        return f"Worker PID {os.getpid()} now holds {len(LEAK) * 100} MB"
