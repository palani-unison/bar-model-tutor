import copy
import json
from pathlib import Path

import pytest
from PIL import Image

from renderer.render import BoardV2, layout_error, make_board, render_still

ROOT = Path(__file__).resolve().parent.parent
V2_EXAMPLES = sorted((ROOT / "specs" / "examples" / "v2").glob("*.json"))
V1_EXAMPLES = sorted((ROOT / "specs" / "examples").glob("*.json"))


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _stamps():
    return _load(ROOT / "specs" / "examples" / "v2" / "stamps-transfer.json")


@pytest.mark.parametrize("path", V2_EXAMPLES, ids=lambda p: p.stem)
def test_every_v2_golden_example_lays_out(path):
    assert layout_error(_load(path)) is None


@pytest.mark.parametrize("path", V2_EXAMPLES[:1] + V1_EXAMPLES[:1], ids=lambda p: p.stem)
def test_render_still_writes_a_png(path, tmp_path):
    out = tmp_path / "board.png"
    render_still(_load(path), out)
    with Image.open(out) as img:
        assert img.format == "PNG" and img.size == (1280, 720)


def test_bad_bar_id_is_a_layout_error():
    spec = _stamps()
    step = next(a for a in spec["timeline"] if a["do"] == "draw_bar")
    step["bar"] = "nobody"
    assert "nobody" in layout_error(spec)


def test_out_of_range_segments_are_a_layout_error():
    spec = _stamps()
    spec["timeline"].insert(-1, {"do": "bracket", "bar": "ali_a", "segments": [0, 5], "side": "top",
                                 "text": "60", "say": "Ali's stamps in the end."})
    assert "out of range" in layout_error(spec)
    spec = _stamps()
    next(a for a in spec["timeline"] if a["do"] == "move_part")["from_segment"] = 4
    assert "out of range" in layout_error(spec)


def test_unknown_stage_and_missing_highlight_are_layout_errors():
    spec = _stamps()
    next(a for a in spec["timeline"] if a["do"] == "draw_stage")["stage"] = "later"
    assert "later" in layout_error(spec)
    spec = _stamps()
    next(a for a in spec["timeline"] if a["do"] == "highlight")["text"] = "5 times as many"
    assert "not in the question" in layout_error(spec)


def test_too_many_bars_dont_fit():
    spec = _stamps()
    bars = [{"quantity": "ali_before", "label": "Ali", "segments": [{"expr": "3*u"}]},
            {"quantity": "ben_before", "label": "Ben", "segments": [{"expr": "u"}]},
            {"quantity": "ben_before", "label": "Cai", "segments": [{"expr": "u"}]}]
    spec["stages"] = [{"id": f"s{s}", "label": f"Stage {s}",
                       "bars": [dict(b, id=f"b{s}{i}") for i, b in enumerate(copy.deepcopy(bars))]}
                      for s in range(3)]
    spec["timeline"] = [a for a in spec["timeline"] if a["do"] in ("write_question", "pause", "write", "answer")]
    assert "don't fit" in layout_error(spec)


def test_long_working_line_doesnt_fit():
    spec = _stamps()
    next(a for a in spec["timeline"] if a["do"] == "write")["text"] = "1 unit = " + " = ".join(["20"] * 14)
    assert "doesn't fit" in layout_error(spec)


def test_bars_share_one_scale_and_removed_parts_count_in_drawn_length():
    board = make_board(_stamps())
    assert isinstance(board, BoardV2)
    # Ali at first (3u) and Ali in the end (2u + a removed 1u) end at the same x.
    assert board.seg_x["ali_b"][-1][1] == pytest.approx(board.seg_x["ali_a"][-1][1])
    assert board.seg_x["ben_b"][-1][1] == pytest.approx(board.seg_x["ben_a"][0][1])
    assert board.bh >= 36


def test_working_template_puts_lines_across_the_full_width():
    spec = _load(ROOT / "specs" / "examples" / "v2" / "sweets-excess-shortage.json")
    board = make_board(spec)
    line = next(e for a in spec["timeline"] if a["do"] == "write" for e in board.elements(a))
    assert line.xy[0] == 60


@pytest.mark.parametrize("path", V1_EXAMPLES, ids=lambda p: p.stem)
def test_v1_layout_still_works(path):
    spec = _load(path)
    assert not isinstance(make_board(spec), BoardV2)
    assert layout_error(spec) is None


def test_render_endpoint_accepts_v2_specs(monkeypatch):
    import json
    import subprocess
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    monkeypatch.setattr("voice.tts.available", lambda: True)

    def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    spec = json.loads((Path(__file__).resolve().parent.parent / "specs/examples/v2/stamps-transfer.json").read_text())
    r = TestClient(main.app).post("/api/render", json={"spec": spec})
    assert r.status_code == 200, r.text
    bad = dict(spec, answer=99)
    assert TestClient(main.app).post("/api/render", json={"spec": bad}).status_code == 422



def test_table_is_drawn_left_of_the_divider_and_working_moves_right():
    spec = _load(ROOT / "specs" / "examples" / "v2" / "assumption-legs-table.json")
    board = BoardV2(spec)
    assert board.table and board.table["xs"][-1] <= 750 and board.work_x == 790


def test_table_that_is_too_wide_is_a_layout_error():
    spec = _load(ROOT / "specs" / "examples" / "v2" / "rate-tap-table.json")
    table = next(a for a in spec["timeline"] if a["do"] == "table")
    wide = "Mmmmmmmmmmmmmmmmmm"  # 18 wide letters in each of 4 columns
    table["columns"] = [wide] * 4
    table["rows"] = [[wide] * 4 for _ in range(3)]
    assert "too wide" in layout_error(spec)


def test_fill_cell_covers_its_question_mark(tmp_path):
    spec = _load(ROOT / "specs" / "examples" / "v2" / "rate-tap-table.json")
    board = BoardV2(spec)
    els = [board.elements(a) for a in spec["timeline"]]
    fill = next(e for a, e in zip(spec["timeline"], els) if a["do"] == "fill_cell")
    assert fill[1].cell == (2, 1) and fill[1].txt == "18 ÷ 3 = 6"



def test_number_line_values_never_run_together():
    spec = _load(ROOT / "specs" / "examples" / "v2" / "speed-two-parts-line.json")
    line = next(a for a in spec["timeline"] if a["do"] == "number_line")
    line["marks"][2]["at"], line["jumps"][1]["to"] = 240, 240  # the second value now sits close to the first
    next(a for a in spec["timeline"] if a["do"] == "mark_point" and a["at"] == 320)["at"] = 240
    slots = BoardV2(spec).line["slots"]
    assert set(slots) == {210, 240} and slots[210][1] != slots[240][1]  # stepped up a row, not run together
    assert layout_error(spec) is None


def test_number_line_lays_out_and_reverse_jumps_draw():
    spec = _load(ROOT / "specs" / "examples" / "v2" / "speed-meeting-line.json")
    assert layout_error(spec) is None
    line = next(a for a in spec["timeline"] if a["do"] == "number_line")
    assert any(j["from"] > j["to"] for j in line["jumps"])


def test_mark_point_without_a_slot_is_a_layout_error():
    spec = _load(ROOT / "specs" / "examples" / "v2" / "speed-meeting-line.json")
    step = next(a for a in spec["timeline"] if a["do"] == "mark_point")
    step["at"] = 300
    assert "mark_point" in layout_error(spec)



def test_percentage_board_labels_each_unit_bar_with_its_percent():
    spec = _load(ROOT / "specs" / "examples" / "v2" / "percentage_change-increase.json")
    tags = {bid: t for bid, (_, t) in BoardV2(spec).percent_tags.items()}
    assert tags == {"old_bar": "100%", "new_bar": "120%"}
    spec.pop("board")
    assert BoardV2(spec).percent_tags == {}


def test_a_long_answer_wraps_onto_two_lines_instead_of_tiny_writing():
    spec = _load(ROOT / "specs" / "examples" / "v2" / "constant_difference-ages.json")
    board = BoardV2(spec)
    els = [board.elements(a) for a in spec["timeline"]][-1]
    lines = [e for e in els if hasattr(e, "txt")]
    assert len(lines) == 2 and all(e.fnt.size >= 30 * 2 for e in lines)
