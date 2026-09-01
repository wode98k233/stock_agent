from unittest.mock import patch

from cli.tui.widgets.confirm_modal import ConfirmModal


def test_modal_stores_inputs():
    applied = []
    cancelled = []
    m = ConfirmModal(
        title="模板保存",
        summary="将修改 sector_rotation",
        diff=["- a", "+ b"],
        backup_path="backups/x.md",
        on_apply=lambda: applied.append(True),
        on_cancel=lambda: cancelled.append(True),
    )
    assert m._title == "模板保存"
    assert m._diff == ["- a", "+ b"]
    assert m._backup_path == "backups/x.md"


import pytest


@pytest.mark.asyncio
async def test_modal_toggle_diff():
    m = ConfirmModal(title="x", summary="y", diff=["- a", "+ b"])
    assert m._show_full_diff is False
    await m.action_toggle_diff()
    assert m._show_full_diff is True
    await m.action_toggle_diff()
    assert m._show_full_diff is False


def test_modal_action_apply_invokes_callback():
    called = []
    m = ConfirmModal(title="x", summary="y", diff=[], on_apply=lambda: called.append("apply"))
    with patch.object(m, "dismiss") as mock_dismiss:
        m.action_apply()
        assert called == ["apply"]
        mock_dismiss.assert_called_once_with(True)


def test_modal_action_cancel_invokes_callback():
    called = []
    m = ConfirmModal(title="x", summary="y", diff=[], on_cancel=lambda: called.append("cancel"))
    with patch.object(m, "dismiss") as mock_dismiss:
        m.action_cancel()
        assert called == ["cancel"]
        mock_dismiss.assert_called_once_with(False)


def test_modal_missing_callback_does_not_raise():
    m = ConfirmModal(title="x", summary="y", diff=[])
    with patch.object(m, "dismiss"):
        m.action_apply()
        m.action_cancel()
