from stb.backends.fake import FakeBackend
from stb.models import LogicValue, ObjectRef, TimeSpec
from stb.service import StbService


def test_core_types_validate() -> None:
    assert TimeSpec(value="12.5", unit="ns").unit.value == "ns"
    assert LogicValue(width=4, value="10xz").value == "10xz"
    ref = ObjectRef(
        model="netlist",
        context_id="rtl",
        worker_generation=1,
        npi_type="DECL_NET",
        full_name="top.req",
    )
    assert ref.full_name == "top.req"


def test_fake_resolve_and_query() -> None:
    service = StbService(FakeBackend("rtl"), "rtl")
    resolved = service.object_resolve({"name": "top.u_core.data"})
    assert resolved["status"] == "complete"
    assert resolved["data"]["semantic_class"] == "register"

    queried = service.object_query(
        {"scope": "top.u_core", "semantic_classes": ["register"], "limit": 10}
    )
    assert queried["data"]["objects"][0]["name"] == "data"


def test_error_is_stable() -> None:
    service = StbService(FakeBackend("rtl"), "rtl")
    response = service.object_resolve({"name": "top.missing"})
    assert response["status"] == "failed"
    assert response["error"]["code"] == "object_not_found"
