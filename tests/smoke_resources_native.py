"""Explicit native image reading from an owned window, no visual locating."""
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import win32gui
from PIL import Image
from gui_api import GuiSession
from probe_universality import launch_owned,close_owned
from verify_discovery import data


def main(output):
    output.mkdir(parents=True,exist_ok=False)
    built=subprocess.run(['powershell.exe','-NoProfile','-File',str(ROOT/'tests/fixtures/Build-ResourceFixture.ps1')],
        cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    assert built.returncode==0,built.stderr
    executable=Path(built.stdout.strip());assert executable.is_file()
    previous=win32gui.GetForegroundWindow();state=output/'window.txt'
    owned=launch_owned([str(executable),str(state)],hidden=False)
    report={'ok':False,'screenshots_for_locating':0,'production_app_writes':0,'model_calls':0};hwnd=None;restore=False
    try:
        deadline=time.monotonic()+10
        while not state.exists() and time.monotonic()<deadline and owned.poll() is None:time.sleep(.05)
        hwnd=int(state.read_text());time.sleep(.2)
        with GuiSession(resource_root=output/'artifacts',discovery_cache=output/'cache.json',learning_cache=output/'learning.json') as gui:
            source=data(gui.request('attach',{'runtime':'native','window':hwnd,'backend':'msaa'}))['source']
            report['window_action']=data(gui.request('window_action',{'source':source,'action':'bring_to_front'}));time.sleep(.3)
            assert win32gui.GetForegroundWindow()==hwnd,'Owned fixture was not foreground; no capture was attempted'
            found=data(gui.request('resources',{'source':source}))
            images=[item for item in found['resources'] if item.get('name')=='Owned resource image']
            assert len(images)==1,found
            item=images[0];assert item['availability']['rendered'] and not item['availability']['source_bytes']
            missing=gui.request('resource_read',{'resource_ref':item['resource_ref']})
            assert missing['error']['code']=='resource_source_unavailable',missing
            captured=data(gui.request('resource_read',{'resource_ref':item['resource_ref'],'representation':'rendered','max_pixels':65536}))
            artifact=captured['artifact']
            with Image.open(artifact['path']) as image:
                image.load();rgb=image.convert('RGB');width,height=image.size
                left=rgb.getpixel((width//4,height//2));right=rgb.getpixel((width*3//4,height//2))
                assert left==(230,40,50) and right==(40,120,220),(left,right)
            assert artifact['representation']=='rendered_region' and not artifact['original_quality_verified']
            report.update(ok=True,provider='msaa',resource_count=len(images),source_not_fabricated=True,
                rendered_artifact=artifact,decoded_pixels_verified=True,width=width,height=height)
    except Exception as exc:report['error']={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
    finally:
        restore=bool(hwnd and win32gui.GetForegroundWindow()==hwnd)
        report['cleanup']=close_owned(owned)
        if restore and previous and win32gui.IsWindow(previous):
            try:win32gui.SetForegroundWindow(previous)
            except Exception:pass
        report['foreground_restore_attempted']=restore
        (output/'native.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True))
    return 0 if report['ok'] and report['cleanup']['closed'] else 1


if __name__=='__main__':raise SystemExit(main(Path(sys.argv[1]).resolve()))
