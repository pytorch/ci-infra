"""Unit tests for gpu_dev.common.errors and gpu_dev.common.enums.

Covers the SDK error class hierarchy/inheritance + the GpuType / ReservationStatus
str-enums and the GPU_MAX_COUNT mapping (full GPUs, small GPUs, MIG slices, CPU).
"""

import pickle

import pytest

from gpu_dev.common.errors import (
    GpuDevError,
    GpuDevAuthError,
    GpuDevNotFoundError,
    GpuDevTimeoutError,
    GpuDevValidationError,
    GpuDevConnectionError,
    GpuDevCapacityError,
)
from gpu_dev.common.enums import GpuType, ReservationStatus, GPU_MAX_COUNT


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

ALL_SUBCLASSES = [
    GpuDevAuthError,
    GpuDevNotFoundError,
    GpuDevTimeoutError,
    GpuDevValidationError,
    GpuDevConnectionError,
    GpuDevCapacityError,
]


def test_base_is_exception_subclass():
    assert issubclass(GpuDevError, Exception)


@pytest.mark.parametrize("cls", ALL_SUBCLASSES)
def test_subclasses_inherit_from_base(cls):
    assert issubclass(cls, GpuDevError)
    assert issubclass(cls, Exception)


@pytest.mark.parametrize("cls", ALL_SUBCLASSES)
def test_subclasses_are_not_each_other(cls):
    # Each error type is distinct; an AuthError should not be a CapacityError, etc.
    others = [c for c in ALL_SUBCLASSES if c is not cls]
    for other in others:
        assert not issubclass(cls, other)


def test_base_constructor_sets_message_and_default_code():
    err = GpuDevError("boom")
    assert err.message == "boom"
    assert err.code is None
    # message is forwarded to Exception args so str() works as expected
    assert str(err) == "boom"
    assert err.args == ("boom",)


def test_base_constructor_with_code():
    err = GpuDevError("nope", code="E_AUTH")
    assert err.message == "nope"
    assert err.code == "E_AUTH"
    assert str(err) == "nope"


def test_code_is_keyword_only():
    # `code` is keyword-only (defined after *), so positional passing must fail.
    with pytest.raises(TypeError):
        GpuDevError("msg", "E_CODE")


@pytest.mark.parametrize("cls", ALL_SUBCLASSES)
def test_subclasses_use_inherited_constructor(cls):
    err = cls("detail", code="C123")
    assert err.message == "detail"
    assert err.code == "C123"
    assert str(err) == "detail"
    assert isinstance(err, GpuDevError)


@pytest.mark.parametrize("cls", ALL_SUBCLASSES)
def test_subclasses_default_code_none(cls):
    err = cls("only message")
    assert err.code is None
    assert err.message == "only message"


def test_can_catch_subclass_as_base():
    try:
        raise GpuDevCapacityError("no gpus", code="CAP")
    except GpuDevError as e:
        caught = e
    assert isinstance(caught, GpuDevCapacityError)
    assert caught.code == "CAP"


def test_can_catch_base_as_exception():
    with pytest.raises(Exception):
        raise GpuDevAuthError("denied")


def test_distinct_subclass_not_caught_by_sibling():
    # Catching one sibling must not accidentally catch another.
    with pytest.raises(GpuDevTimeoutError):
        try:
            raise GpuDevTimeoutError("slow")
        except GpuDevAuthError:  # wrong sibling, must not match
            pytest.fail("sibling error type wrongly caught")


def test_raise_and_attributes_preserved_on_reraise():
    err = GpuDevValidationError("bad arg", code="V1")
    with pytest.raises(GpuDevValidationError) as excinfo:
        raise err
    assert excinfo.value.message == "bad arg"
    assert excinfo.value.code == "V1"


def test_all_error_classes_are_distinct_types():
    classes = [GpuDevError] + ALL_SUBCLASSES
    assert len(set(classes)) == len(classes)


def test_error_is_picklable_message():
    # message is forwarded as the single Exception arg, so pickling round-trips it.
    err = GpuDevError("serialize me")
    restored = pickle.loads(pickle.dumps(err))
    assert isinstance(restored, GpuDevError)
    assert str(restored) == "serialize me"


# ---------------------------------------------------------------------------
# GpuType enum
# ---------------------------------------------------------------------------

EXPECTED_GPU_TYPE_VALUES = {
    "H100": "h100",
    "H200": "h200",
    "B200": "b200",
    "B300": "b300",
    "A100": "a100",
    "T4": "t4",
    "L4": "l4",
    "A10G": "a10g",
    "RTX_PRO_6000": "rtxpro6000",
    "H100_MIG_1G": "h100-mig-1g",
    "H100_MIG_2G": "h100-mig-2g",
    "H100_MIG_3G": "h100-mig-3g",
    "B200_MIG_1G": "b200-mig-1g",
    "B200_MIG_2G": "b200-mig-2g",
    "B200_MIG_3G": "b200-mig-3g",
    "CPU_ARM": "cpu-arm",
    "CPU_X86": "cpu-x86",
}


def test_gputype_is_str_enum():
    assert issubclass(GpuType, str)
    # A str-Enum member compares equal to its raw string value.
    assert GpuType.H100 == "h100"
    assert GpuType.H100.value == "h100"


def test_gputype_member_set_complete():
    actual = {m.name: m.value for m in GpuType}
    assert actual == EXPECTED_GPU_TYPE_VALUES


@pytest.mark.parametrize("name,value", list(EXPECTED_GPU_TYPE_VALUES.items()))
def test_gputype_lookup_by_value(name, value):
    assert GpuType(value) is getattr(GpuType, name)
    assert GpuType(value).value == value


def test_gputype_unique_values():
    values = [m.value for m in GpuType]
    assert len(values) == len(set(values))


def test_gputype_invalid_value_raises():
    with pytest.raises(ValueError):
        GpuType("a6000")


def test_gputype_is_case_sensitive():
    # Source values are lowercase; uppercase must not resolve.
    with pytest.raises(ValueError):
        GpuType("H100")


def test_gputype_string_behaviour():
    # Being a str subclass, the member can be used directly in string contexts.
    assert GpuType.CPU_ARM.value == "cpu-arm"
    assert f"type={GpuType.T4.value}" == "type=t4"
    assert GpuType.B200.startswith("b2")


def test_gputype_mig_and_cpu_members_present():
    mig = [m for m in GpuType if "mig" in m.value]
    cpu = [m for m in GpuType if m.value.startswith("cpu-")]
    assert {m.value for m in mig} == {
        "h100-mig-1g", "h100-mig-2g", "h100-mig-3g",
        "b200-mig-1g", "b200-mig-2g", "b200-mig-3g",
    }
    assert {m.value for m in cpu} == {"cpu-arm", "cpu-x86"}


# ---------------------------------------------------------------------------
# ReservationStatus enum
# ---------------------------------------------------------------------------

EXPECTED_STATUS_VALUES = {
    "PENDING": "pending",
    "QUEUED": "queued",
    "PREPARING": "preparing",
    "ACTIVE": "active",
    "CANCELLED": "cancelled",
    "EXPIRED": "expired",
    "FAILED": "failed",
}


def test_reservation_status_is_str_enum():
    assert issubclass(ReservationStatus, str)
    assert ReservationStatus.ACTIVE == "active"
    assert ReservationStatus.ACTIVE.value == "active"


def test_reservation_status_member_set_complete():
    actual = {m.name: m.value for m in ReservationStatus}
    assert actual == EXPECTED_STATUS_VALUES


@pytest.mark.parametrize("name,value", list(EXPECTED_STATUS_VALUES.items()))
def test_reservation_status_lookup_by_value(name, value):
    assert ReservationStatus(value) is getattr(ReservationStatus, name)


def test_reservation_status_invalid_raises():
    with pytest.raises(ValueError):
        ReservationStatus("running")


def test_reservation_status_unique_values():
    values = [m.value for m in ReservationStatus]
    assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# GPU_MAX_COUNT mapping
# ---------------------------------------------------------------------------

def test_gpu_max_count_keys_cover_every_gputype():
    # Every GpuType value must have a max-count entry, and there should be no
    # extra/stale keys.
    enum_values = {m.value for m in GpuType}
    assert set(GPU_MAX_COUNT.keys()) == enum_values


def test_gpu_max_count_values_are_ints():
    for k, v in GPU_MAX_COUNT.items():
        assert isinstance(v, int), f"{k} -> {v!r}"
        assert not isinstance(v, bool)  # guard: bools are ints in python


def test_gpu_max_count_full_gpus():
    for t in ("h100", "h200", "b200", "b300", "a100"):
        assert GPU_MAX_COUNT[t] == 8


def test_gpu_max_count_small_gpus():
    for t in ("t4", "l4", "a10g", "rtxpro6000"):
        assert GPU_MAX_COUNT[t] == 4


def test_gpu_max_count_mig_slices_are_one():
    mig_keys = [k for k in GPU_MAX_COUNT if "mig" in k]
    assert len(mig_keys) == 6
    for k in mig_keys:
        assert GPU_MAX_COUNT[k] == 1


def test_gpu_max_count_cpu_is_zero():
    assert GPU_MAX_COUNT["cpu-arm"] == 0
    assert GPU_MAX_COUNT["cpu-x86"] == 0


def test_gpu_max_count_accessible_by_enum_value():
    # Because GpuType is a str enum, members can index the dict directly.
    assert GPU_MAX_COUNT[GpuType.H100] == 8
    assert GPU_MAX_COUNT[GpuType.T4] == 4
    assert GPU_MAX_COUNT[GpuType.H100_MIG_2G] == 1
    assert GPU_MAX_COUNT[GpuType.CPU_X86] == 0


def test_gpu_max_count_no_negative_values():
    assert all(v >= 0 for v in GPU_MAX_COUNT.values())


def test_gpu_max_count_entry_total():
    # 17 GPU types total -> 17 entries.
    assert len(GPU_MAX_COUNT) == 17
    assert len(GPU_MAX_COUNT) == len(list(GpuType))
