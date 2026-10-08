from pathlib import Path

from ui.instance import UiInstance


def test_same_checkout_reuses_owned_port_and_releases_on_exit(tmp_path: Path):
    with UiInstance(tmp_path, 7860) as owner:
        assert owner.owned
        with UiInstance(tmp_path, 7899) as duplicate:
            assert not duplicate.owned
            assert duplicate.port == 7860
    with UiInstance(tmp_path, 7860) as next_run:
        assert next_run.owned


def test_another_checkout_has_an_independent_lock(tmp_path: Path):
    with UiInstance(tmp_path / "a", 7860) as first, UiInstance(tmp_path / "b", 7861) as second:
        assert first.owned and second.owned


def test_held_lock_requires_a_healthy_app_response(tmp_path: Path, monkeypatch):
    import io

    from ui import instance

    with UiInstance(tmp_path, 7860) as owner:
        assert owner.owned
        with UiInstance(tmp_path, 7860) as duplicate:
            monkeypatch.setattr(
                instance.urllib.request,
                "urlopen",
                lambda *args, **kwargs: io.StringIO('{"title":"vlogsrt 원본 브라우저"}'),
            )
            assert duplicate.wait_until_listening(timeout=0.01)

            def unavailable(*args, **kwargs):
                raise OSError("server is shutting down")

            monkeypatch.setattr(instance.urllib.request, "urlopen", unavailable)
            assert not duplicate.wait_until_listening(timeout=0.01)
