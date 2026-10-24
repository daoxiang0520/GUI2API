"""An ordinary Qt widgets app. State file is only an independent test observer."""
import json
import os
from pathlib import Path
import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton, QLineEdit,
    QCheckBox, QListWidget, QLabel, QTabWidget, QTreeWidget, QTreeWidgetItem, QSlider)
from PyQt5.QtCore import Qt

app = QApplication(sys.argv[:1])
app.setApplicationName('AppCtl Qt control objects')
window = QWidget()
window.setWindowTitle('AppCtl Qt control objects')
window.resize(580, 700)
layout = QVBoxLayout(window)

edit = QLineEdit(); edit.setAccessibleName('Text entry'); edit.setObjectName('TextEntry')
button = QPushButton('Increment'); button.setAccessibleName('Increment')
check = QCheckBox('Option'); check.setAccessibleName('Option')
choices = QListWidget(); choices.setAccessibleName('Choices'); choices.addItems(['First', 'Second', 'Third'])
choices.setMaximumHeight(100)
tabs = QTabWidget(); tabs.setAccessibleName('Sections')
tabs.addTab(QLabel('Alpha page'), 'Alpha'); tabs.addTab(QLabel('Beta page'), 'Beta')
tree = QTreeWidget(); tree.setAccessibleName('Tree'); tree.setHeaderHidden(True)
parent = QTreeWidgetItem(tree, ['Parent']); QTreeWidgetItem(parent, ['Child'])
tree.setMaximumHeight(120)
slider = QSlider(Qt.Horizontal); slider.setAccessibleName('Level'); slider.setRange(0, 100)
counter = QLabel('0'); counter.setAccessibleName('Counter')
for widget in (edit, button, check, choices, tabs, tree, slider, counter): layout.addWidget(widget)

count = 0
state_path = Path(sys.argv[1])
def save(*_):
    data = {'window': int(window.winId()), 'pid': os.getpid(), 'count': count, 'text': edit.text(),
        'checked': check.isChecked(), 'selected': choices.currentRow(),
        'selected_rows': sorted(choices.row(item) for item in choices.selectedItems()), 'tab': tabs.currentIndex(),
        'expanded': parent.isExpanded(), 'level': slider.value()}
    temporary = state_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    temporary.replace(state_path)
def increment():
    global count
    count += 1
    counter.setText(str(count)); save()
button.clicked.connect(increment)
for signal in (edit.textChanged, check.toggled, choices.currentRowChanged, choices.itemSelectionChanged, tabs.currentChanged,
               tree.itemExpanded, tree.itemCollapsed, slider.valueChanged): signal.connect(save)
window.show()
QTimer.singleShot(0, save)
sys.exit(app.exec_())
