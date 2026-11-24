from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.raw.test_visual_workbench_ui import _NODE_HARNESS
from ui import ui_facade


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_source_overlay_native_and_fallback_use_source_clock_and_live_revision(tmp_path):
    script = r"""
const overlayModel = visualModel();
overlayModel.epoch='overlay-1'; overlayModel.token='overlay-token';
overlayModel.sources.srcA.subtitles=[{id:'u1',start:'1/2',end:'3/2',text:'<b>A & caption</b>'}];
overlayModel.sources.srcA.transcriptRevision='t1';
overlayModel.sources.srcB.subtitles=[{id:'u2',start:'3/2',end:'7/4',text:'B caption'}];
overlayModel.sources.srcB.transcriptRevision='tb1';
const ov=visualEnv({},overlayModel);
ov.registry['visual-source-caption']=el({id:'visual-source-caption'});
ov.registry['vl-source-caption-toggle']=el({id:'vl-source-caption-toggle'});
ov.rewired();
const overlay=ov.registry['visual-source-caption'];
const origin=VL.toFloat(VL.parse(overlayModel.sources.srcA.origin));
ov.vid.tick(origin+1);
if(overlay.textContent!=='<b>A & caption</b>' ||
  overlay.innerHTML!=='')
  throw Error('fallback text must be safe, source timed');
ov.vid.pauseEvent();
if(overlay.textContent!=='<b>A & caption</b>')
  throw Error('pause lost caption');
ov.vid.state.currentTime=origin+1.5;
(ov.vid.listeners.seeked||[]).forEach(f=>f());
if(overlay.textContent!=='')
  throw Error('end must be exclusive after seek');
ov.vid.state.currentTime=origin+.5;
(ov.vid.listeners.seeking||[]).forEach(f=>f());
if(!overlay.textContent.includes('A & caption'))
  throw Error('backseek lost caption');
ov.vid.video.playbackRate=2;
(ov.vid.listeners.ratechange||[]).forEach(f=>f());
if(!overlay.textContent.includes('A & caption'))
  throw Error('rate change shifted source time');
overlayModel.sources.srcA.subtitles[0]={id:'u1',start:'3/4',end:'5/4',text:'edited <literal>'};
overlayModel.sources.srcA.transcriptRevision='t2';
ov.vid.state.currentTime=origin+1;
ov.root.setAttribute('data-vl-model',JSON.stringify(overlayModel));
const srcSets=ov.vid.calls.srcSets.length;
ov.rewired();
if(overlay.textContent!=='edited <literal>' ||
  ov.vid.calls.srcSets.length!==srcSets)
  throw Error('caption edit did not update paused frame');
ov.click(el({attrs:{'data-vl-card':'segB1'}}));
if(overlay.textContent!=='B caption')
  throw Error('source B inherited A caption');
ov.click(el({attrs:{'data-vl-card':'segC1'}}));
if(overlay.textContent!=='')
  throw Error('unavailable source retained old caption');
ov.click(el({attrs:{'data-vl-card':'segA1'}}));
windowScope.VTTCue=function(start,end,text){this.startTime=start;this.endTime=end;this.text=text;};
let native={cues:[],mode:'disabled',addCue(c){this.cues.push(c)},
  removeCue(c){this.cues.splice(this.cues.indexOf(c),1)}};
ov.vid.video.addTextTrack=()=>native;
ov.rewired();
if(native.mode!=='showing' ||
  native.cues.length!==1)
  throw Error('native subtitles not enabled by default');
if(Math.abs(native.cues[0].startTime-(origin+.75))>1e-8 ||
  Math.abs(native.cues[0].endTime-(origin+1.25))>1e-8)
  throw Error('native cue media origin incorrect');
if(native.cues[0].text!=='edited &lt;literal&gt;')
  throw Error('native cue escaping');
if(overlay.textContent!=='')
  throw Error('native and fallback captions overlap');
ov.click(el({attrs:{'data-vl-card':'segB1'}}));
if(native.cues.length!==1 ||
  native.cues[0].text!=='B caption')
  throw Error('native source reset');
if(Math.abs(native.cues[0].startTime-2.021)>1e-8)
  throw Error('native B source offset');
(ov.registry['vl-source-caption-toggle']._listeners.click||[]).forEach(f=>f());
if(native.mode!=='disabled')
  throw Error('caption toggle failed');
(ov.registry['vl-source-caption-toggle']._listeners.click||[]).forEach(f=>f());
if(native.mode!=='showing')
  throw Error('caption toggle restore failed');
checkpoints.push({case:'source-overlay-verified',native:true,fallback:true});
"""
    harness = tmp_path / "overlay.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            script + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
        )
    )
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"visual": ui_facade.VISUAL_BOOTSTRAP_JS}))
    result = subprocess.run(
        ["node", str(harness), str(payload)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr[-2500:]
    assert any(r.get("case") == "source-overlay-verified" for r in json.loads(result.stdout))
