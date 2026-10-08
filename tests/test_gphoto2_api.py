"""The fake in tests/fake_gphoto2.py must not drift from the real binding:
every name stormwatch.camera uses has to exist in python-gphoto2."""
import pytest

gp = pytest.importorskip("gphoto2")


def test_names_used_by_camera_worker_exist():
    for name in ["Camera", "PortInfoList", "CameraAbilitiesList", "GPhoto2Error", "check_result",
                 "gp_camera_autodetect", "GP_EVENT_TIMEOUT", "GP_EVENT_FILE_ADDED",
                 "GP_FILE_TYPE_NORMAL", "GP_WIDGET_RADIO", "GP_WIDGET_MENU"]:
        assert hasattr(gp, name), name
    for meth in ["init", "exit", "set_port_info", "set_abilities", "trigger_capture",
                 "wait_for_event", "capture_preview", "file_get_info", "file_read", "file_get",
                 "file_delete", "get_single_config", "set_single_config", "get_config", "set_config"]:
        assert hasattr(gp.Camera, meth), meth
    assert hasattr(gp.PortInfoList, "lookup_path") and hasattr(gp.CameraAbilitiesList, "lookup_model")


def test_constants_match_the_fake():
    import fake_gphoto2 as fake

    for name in ["GP_EVENT_TIMEOUT", "GP_EVENT_FILE_ADDED", "GP_FILE_TYPE_NORMAL",
                 "GP_WIDGET_TEXT", "GP_WIDGET_TOGGLE", "GP_WIDGET_RADIO", "GP_WIDGET_MENU"]:
        assert getattr(gp, name) == getattr(fake, name), name
    from stormwatch import camera

    assert gp.GP_ERROR_IO_USB_CLAIM == camera.GP_ERROR_IO_USB_CLAIM
    assert gp.GP_ERROR_CAMERA_BUSY == camera.GP_ERROR_CAMERA_BUSY
    assert gp.GP_ERROR_NOT_SUPPORTED == camera.GP_ERROR_NOT_SUPPORTED
    assert gp.GP_ERROR_BAD_PARAMETERS == camera.GP_ERROR_BAD_PARAMETERS
    assert gp.GP_ERROR_MODEL_NOT_FOUND == camera.GP_ERROR_MODEL_NOT_FOUND


def test_real_autodetect_with_no_camera_returns_a_camera_list():
    cl = gp.check_result(gp.gp_camera_autodetect())
    assert cl.count() >= 0
    if cl.count():
        cl.get_name(0), cl.get_value(0)
