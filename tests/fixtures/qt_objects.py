from PyQt5.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot


class QtCounter(QObject):
    changed = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._count = 0
        self._label = "Qt 业务对象"

    @pyqtProperty(int, notify=changed)
    def count(self):
        return self._count

    @pyqtProperty(str)
    def label(self):
        return self._label

    @label.setter
    def label(self, value):
        self._label = value

    @pyqtSlot(int, result=int)
    def add(self, amount):
        self._count += amount
        self.changed.emit(self._count)
        return self._count

    @pyqtSlot(str, result=str)
    def echo(self, text):
        return text


def create_roots():
    counter = QtCounter()
    return {"counter": counter, "same_counter": counter}
