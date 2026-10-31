import pytest

from tftreview.cli import main


@pytest.fixture
def run(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TFTREVIEW_HOME", str(tmp_path / "home"))
    db = str(tmp_path / "lib.sqlite3")

    def _run(*argv):
        code = main(["--db", db, *argv])
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def test_end_to_end_review_workflow(run, tmp_path):
    video = str(tmp_path / "game1.mp4")
    assert run("new", video)[0] == 0
    for label, t, text in (("3-1", "10:00", "gold=40"), ("3-2", "10:30", "gold=12 #rolldown")):
        assert run("stamp", "game1", label, t)[0] == 0
        assert run("add", "game1", "--event", label, text)[0] == 0
    assert run("add", "game1", "--game-level", "place=2 #fast9")[0] == 0

    code, out, _ = run("events", "game1")
    assert code == 0 and "3-1" in out and "3-2" in out and "next expected round: 3-3" in out

    code, out, _ = run("analyze", "metric", "gold")
    assert code == 0 and "3-1" in out and "40" in out

    code, out, _ = run("analyze", "compare", "gold", "rolldown")
    assert "mean difference: -28" in out

    code, out, _ = run("analyze", "corr", "gold", "game:place")
    assert code == 0 and "n=2" in out

    csv_path = str(tmp_path / "out.csv")
    assert run("export", "csv", csv_path)[0] == 0
    assert "gold" in open(csv_path).read()

    code, out, _ = run("analyze", "tags")
    assert "#fast9" in out and "#rolldown" in out


def test_errors_are_reported_not_raised(run):
    code, _, err = run("events", "nope")
    assert code == 1 and "no game matches" in err
    run("new", "/v/a.mp4")
    code, _, err = run("add", "a", "--event", "9-9", "x")
    assert code == 1 and "no round" in err
    code, _, err = run("stamp", "a", "banana", "1:00")
    assert code == 1 and "not a round label" in err


def test_hotkeys_and_rounds_commands(run):
    code, out, _ = run("hotkeys")
    assert code == 0 and "Play / pause" in out
    code, out, _ = run("rounds")
    assert code == 0 and '"stages"' in out


def test_detect_dry_run_then_write(run, tmp_path):
    import shutil

    import pytest

    pytest.importorskip("cv2")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is needed to build and scan the test video")
    from synth import make_hud_video

    video = make_hud_video(tmp_path / "hud.mp4", [0] * 20 + [1] * 20 + [2] * 20)

    code, out, err = run("detect", video)
    assert code == 0, err
    assert "1-1" in out and "1-2" in out and "1-3" in out
    assert "dry run: nothing written" in err
    assert run("events", "hud")[0] == 1                # nothing was stored

    code, out, _ = run("detect", video, "--write")
    assert code == 0 and "3 added" in out
    code, out, _ = run("events", "hud")
    assert code == 0 and "1-3" in out

    code, out, _ = run("detect", video, "--write")     # hand-marked rounds are kept
    assert "0 added" in out and "3 kept" in out


def test_read_metrics_needs_rounds(run, tmp_path):
    import pytest

    pytest.importorskip("cv2")
    video = str(tmp_path / "game.mp4")
    assert run("new", video)[0] == 0
    code, _, err = run("read-metrics", "game")
    assert code == 1 and "no rounds" in err


def test_read_metrics_rejects_unknown_round(run, tmp_path):
    import pytest

    pytest.importorskip("cv2")
    video = str(tmp_path / "game.mp4")
    assert run("new", video)[0] == 0
    assert run("stamp", "game", "3-1", "1:00")[0] == 0
    code, _, err = run("read-metrics", "game", "--round", "9-9")
    assert code == 1 and "no such round" in err
