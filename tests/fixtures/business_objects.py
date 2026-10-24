from object_bridge import Event, exposed


class Child:
    def __init__(self):
        self.label = "子对象"

    @exposed(read_only=True)
    def echo(self, text: str) -> str:
        print("fixture application log")
        return text


class Counter:
    def __init__(self):
        self.count = 0
        self.label = "业务对象"
        self.changed = Event()
        self._child = Child()
        self._getter_calls = 0

    @property
    def probe(self):
        self._getter_calls += 1
        return self._getter_calls

    def add(self, amount: int) -> int:
        self.count += amount
        self.changed.emit(self.count)
        return self.count

    @exposed(read_only=True)
    def child(self):
        return self._child

    @exposed(read_only=True)
    def same_child(self, other) -> bool:
        return other is self._child

    @exposed(read_only=True)
    def echo(self, text: str) -> str:
        print("fixture application log")
        return text

    def fail_after_add(self):
        self.count += 1
        raise RuntimeError("fixture failure after a side effect")


def create_roots():
    counter = Counter()
    return {"counter": counter, "same_counter": counter}
