from types import SimpleNamespace as NS

import pytest

from bridges.com_agent import ComAdapter
from object_bridge import Fault


class TypeInfo:
    def __init__(self, name, functions=(), bases=()):
        self.name, self.functions, self.bases = name, list(functions), list(bases)

    def GetTypeAttr(self):
        return NS(guid=self.name, typekind=4, cFuncs=len(self.functions), cImplTypes=len(self.bases))

    def GetDocumentation(self, _):
        return (self.name,)

    def GetFuncDesc(self, index):
        return self.functions[index]

    def GetNames(self, dispid):
        return next(f.names for f in self.functions if f.memid == dispid)

    def GetImplTypeFlags(self, index):
        return self.bases[index][1]

    def GetRefTypeOfImplType(self, index):
        return index

    def GetRefTypeInfo(self, index):
        return self.bases[index][0]


def member(name, dispid, invkind=1, args=()):
    return NS(names=[name], memid=dispid, invkind=invkind, args=args, rettype=(8,), wFuncFlags=0)


def test_inherited_members_are_available_but_outgoing_events_are_not_callable():
    base = TypeInfo("Base", [member("ReadChild", 1)])
    events = TypeInfo("Outgoing", [member("OnChanged", 2)])
    derived = TypeInfo("Derived", [member("Direct", 3)], [(base, 0), (events, 2)])
    base.bases = [(derived, 0)]  # a malformed provider cycle must remain bounded
    obj = NS(_oleobj_=NS(GetTypeInfo=lambda: derived))
    result = ComAdapter().describe(obj)
    assert {m["name"] for m in result["methods"]} == {"ReadChild", "Direct"}
    assert result["interfaces"] == ["Derived", "Base"]


def test_opt_in_metadata_cache_does_not_cache_values_and_is_revoked_on_release():
    calls = {"metadata": 0, "value": 0}
    info = TypeInfo("Mutable", [member("Value", 1, 2)])

    def get_info():
        calls["metadata"] += 1
        return info

    def invoke(*_):
        calls["value"] += 1
        return calls["value"]

    obj = NS(_oleobj_=NS(GetTypeInfo=get_info, Invoke=invoke))
    adapter = ComAdapter(cache_metadata=True)
    adapter.identity = lambda _: 1
    adapter.identities[1] = obj
    assert adapter.read(obj, "Value") == 1
    assert adapter.read(obj, "Value") == 2
    assert calls["metadata"] == 1
    adapter.forget_identity(obj)
    assert adapter.read(obj, "Value") == 3
    assert calls["metadata"] == 2


def test_com_retval_is_return_metadata_rather_than_a_required_input():
    function = member("Child", 5, args=((12, 1), (9, 10)))
    function.names = ["Child", "index", "result"]
    info = TypeInfo("Inherited", [function])
    obj = NS(_oleobj_=NS(GetTypeInfo=lambda: info))
    method = ComAdapter().describe(obj)["methods"][0]
    assert [p["name"] for p in method["parameters"]] == ["index"]
    assert method["returns"] == "9"
    assert method["out_parameters"][0]["name"] == "result"


def test_unimplemented_explicit_output_marshaling_does_not_invoke_the_application():
    info = TypeInfo("Native", [member("Bounds", 6, args=((3, 2),))])
    obj = NS(_oleobj_=NS(GetTypeInfo=lambda: info))  # deliberately has no Invoke
    adapter = ComAdapter()
    assert adapter.describe(obj)["methods"][0]["callable"] is False
    with pytest.raises(Fault, match="by-reference output"):
        adapter.call(obj, "Bounds", [])
