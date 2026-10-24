"""Owned WinForms acceptance across native channels, including mutable labels.

No personal applications, screenshots, model requests, or external messages.
"""
import json
from pathlib import Path
import subprocess
import time
import uuid

from gui_controls import GUI
from probe_universality import launch_owned, close_owned

ROOT=Path(__file__).resolve().parent


def main():
    executable=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',str(ROOT/'tests/fixtures/Build-NativeFixture.ps1')],
        capture_output=True,text=True,encoding='utf-8',check=True).stdout.strip().splitlines()[-1]
    report={'ok':False,'screenshots':0,'messages_sent':0,'runtime_injection':False,'cases':[]}
    for backend in ('msaa','uia3','win32'):
        run=ROOT/'tests/runs'/('native-input-'+uuid.uuid4().hex[:8]);run.mkdir(parents=True)
        state=run/'state.json'
        owned=launch_owned([executable,str(state),'--mutable-name'])
        case={'backend':backend,'ok':False}
        try:
            deadline=time.monotonic()+10
            while not state.exists() and time.monotonic()<deadline:time.sleep(.03)
            def actual(): return json.loads(state.read_text(encoding='utf-8-sig'))
            def until(predicate):
                deadline=time.monotonic()+3
                while True:
                    try:
                        value=actual()
                        if predicate(value):return value
                    except (OSError,ValueError):pass
                    if time.monotonic()>deadline: raise AssertionError('Independent fixture observer did not see the change')
                    time.sleep(.02)
            with GUI() as gui:
                w=gui.window(window=actual()['window'],backend=backend)
                edit=w.one(role='Edit',method='type_text',**({'name':'标准输入框'} if backend=='msaa' else {}))
                button=w.one(name='计数增加',role='Button')
                edit.click_input()
                text='Native '+backend+' 中文🙂'
                edit.type_text(text,replace=True)
                until(lambda value:value['standard_text']==text)
                # The fixture changed AccessibleName; the held object must still work.
                assert edit.read()['value']==text
                edit.press_keys('CTRL+A')
                edit.type_text('标签变化后仍可输入')
                until(lambda value:value['standard_text']=='标签变化后仍可输入')
                button.click_input();until(lambda value:value['count']==1)
                button.press_keys('SPACE');until(lambda value:value['count']==2)
                case.update(ok=True,unicode_input_verified=True,mutable_label_reference_verified=True,
                    click_and_key_effects_verified=True,observed=actual())
        except Exception as exc:case['error']=str(exc)
        finally:case['cleanup']=close_owned(owned)
        report['cases'].append(case)
    report['ok']=all(case['ok'] for case in report['cases'])
    output=ROOT/'reports/qq-native-input-20261024/native-fixtures.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
