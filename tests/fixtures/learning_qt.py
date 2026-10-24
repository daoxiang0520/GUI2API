"""Ordinary Qt window whose details widgets are created on demand."""
import json
import os
from pathlib import Path
import sys
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication,QWidget,QVBoxLayout,QPushButton,QLineEdit

app=QApplication(sys.argv[:1]); window=QWidget(); window.setWindowTitle('AppCtl learning Qt fixture')
layout=QVBoxLayout(window); opener=QPushButton('Open details'); opener.setAccessibleName('Open details'); opener.setObjectName('OpenDetails'); layout.addWidget(opener)
details=None; opens=closes=0; state=Path(sys.argv[1])
def save():
    temporary=state.with_suffix('.tmp')
    temporary.write_text(json.dumps({'window':int(window.winId()),'pid':os.getpid(),'open':details is not None,'opens':opens,'closes':closes}),encoding='utf-8')
    temporary.replace(state)
def close_details():
    global details,closes
    closes+=1; layout.removeWidget(details); details.hide(); details.deleteLater(); details=None; save()
def open_details():
    global details,opens
    if details is not None:return
    opens+=1; details=QWidget(); details.setObjectName('DetailsPanel'); details.setAccessibleName('Details panel')
    inner=QVBoxLayout(details); entry=QLineEdit(); entry.setAccessibleName('Detail entry'); entry.setObjectName('DetailEntry'); inner.addWidget(entry)
    closer=QPushButton('Close details'); closer.setAccessibleName('Close details'); closer.setObjectName('CloseDetails'); closer.clicked.connect(close_details); inner.addWidget(closer)
    layout.addWidget(details); save()
opener.clicked.connect(open_details); window.resize(480,280); window.show(); QTimer.singleShot(0,save)
sys.exit(app.exec_())
