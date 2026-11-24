"""REA로 본 Final Cut · Subtitle Edit 빈칸을 재생기에 잇는다(W-93, D164) — 배선은 실행본 글로 잰다.

키 판정은 `test_keymap.py`(노드), 모델 연산(커서 나누기 · 레인 맞춤)은 `test_timeline_edit_js.py` ·
`test_caption_lane.py`가 노드에서 잰다. 여기서는 재생기가 그 연산만 쓰는지와 안전 장치를 본다:

- 셔틀: 앞은 영상 배속, 뒤는 설정 간격마다 시크 · 멈춤은 배속을 1로 되돌린다.
- 범위: ⌫는 범위가 있으면 모델의 리플 삭제 하나 · 타임라인 전체는 지우지 않는다.
- 보기: 줌 · 맞춤은 레인 보기만 바꾸고 문서를 건드리지 않는다 · 스내핑을 끄면 컷에 붙지 않는다.
- ⌘E는 공유 칸으로 갈 뿐 렌더를 걸지 않는다.
- 오른쪽 누름은 플레이헤드를 옮기지 않고 메뉴는 키 · 단추와 같은 연산이다.
"""

from __future__ import annotations

from ui.timeline_player_js import PLAYER_APP_JS as JS


def _body(head: str) -> str:
    start = JS.index(head)
    end = JS.index("\n  function ", start + len(head))
    return JS[start:end]


def test_the_shuttle_plays_faster_forward_and_seeks_backward_on_the_settings_tick() -> None:
    shuttle = _body("function shuttle(dir) {")
    assert "KEYS.shuttle(S.shuttle, dir, S.settings.shuttleRates)" in shuttle
    assert "v.playbackRate = next.rate" in shuttle
    reverse = _body("function startReverse() {")
    assert "S.settings.reverseTickMs" in reverse and "seek(S.playhead - frames)" in reverse
    assert "S.rootEl.offsetParent === null" in reverse  # 숨은 화면에서는 멈춘다
    stop = _body("function stopPlayback() {")
    assert "stopReverse();" in stop and "S.shuttle = null;" in stop
    assert "v.playbackRate = 1;" in stop
    assert "v.playbackRate = shuttleRate();" in _body("function play() {")
    assert "reversing()" in _body("function toggle() {")
    assert "reversing()" in _body("function step(frames) {")


def test_caption_steps_walk_the_settings_milliseconds_as_sequence_frames() -> None:
    walk = _body("function stepMs(ms) {")
    assert "Math.round(ms * TL.msTicks(S.doc.facts) / seqTicks())" in walk
    assert "S.settings.captionStepMs" in JS and "S.settings.captionSmallStepMs" in JS


def test_delete_with_a_range_is_one_ripple_delete_and_never_the_whole_timeline() -> None:
    assert 'if (S.range) { deleteRange(); } else { clipOp("delete"); }' in JS
    delete = _body("function deleteRange() {")
    assert "TL.rippleDelete(S.doc, got[0], got[1])" in delete
    assert "got[0] === 0 && got[1] === S.total" in delete
    assert delete.index("got[1] === S.total") < delete.index("TL.rippleDelete")
    assert "change(next, " in delete
    mark = _body("function markRange(side) {")
    assert "S.playhead + 1" in mark  # O는 플레이헤드 아래 프레임까지 든다


def test_view_keys_change_only_the_lane_view_and_snapping_off_skips_cut_snaps() -> None:
    for head in ("function zoomLane(factor) {", "function fitLane() {"):
        view = _body(head)
        assert "S.lane = LN." in view and "change(" not in view and "S.doc =" not in view
    assert "S.settings.zoomStepPercent / 100" in JS
    assert "ev.shiftKey || S.snapOff ? ticks" in JS


def test_command_e_goes_to_the_share_cell_and_does_not_render() -> None:
    share = _body("function focusShare() {")
    assert '$("tl-burn")' in share and "focus()" in share
    assert "render(" not in share and "tl_render" not in share


def test_the_caret_split_commits_first_and_repaints_the_editor() -> None:
    split = _body("function splitAtCaret(ev) {")
    assert split.index("area.selectionStart") < split.index("commitText();")
    assert split.index("commitText();") < split.index('cueOp("split", id, caret)')
    assert "paintCueEditor(true)" in split
    # 고치는 중인 입력칸은 다시 그리기가 덮지 않으므로 나눈 왼쪽 글을 직접 넣는다 — 그러지 않으면
    # 칸에 남은 온 글이 다음 확정에서 왼쪽 줄로 되돌아간다.
    assert "fresh.value = kept[4];" in split


def test_the_right_button_menu_keeps_the_playhead_and_reuses_the_cue_operations() -> None:
    menu = _body("function openMenu(ev, id) {")
    assert "seek(" not in menu and "pickCue(" not in menu
    assert "S.cue = id;" in menu
    run = _body("function runMenu(action, id) {")
    assert "cueOp(action, id)" in run and "insertSelection()" in run and "extendToNext()" in run
    assert "ev.button === 2" in _body("function onLaneDown(ev) {")
    assert 'lane.addEventListener("contextmenu", onLaneMenu)' in JS
    assert 'list.addEventListener("contextmenu", onListMenu)' in JS
    assert "menu.contains(ev.target)" in JS  # 메뉴 안의 누름은 메뉴를 닫지 않는다
    assert JS.index('if (S.menu && ev.code === "Escape")') < JS.index("var laneKey =")
