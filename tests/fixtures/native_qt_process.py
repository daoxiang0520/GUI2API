"""Owned native Qt metadata fixture: no windows and no personal app data."""
import json
import os

from PyQt5.QtCore import QCoreApplication, QTimer
from PyQt5 import sip

app = QCoreApplication([])
timer = QTimer(app)
print(json.dumps({'ready': True, 'pid': os.getpid(), 'application': int(sip.unwrapinstance(app)), 'timer': int(sip.unwrapinstance(timer))}), flush=True)
app.exec()
