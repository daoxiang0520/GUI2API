import ctypes

import pytest

from gui_input import NativeInput, _Input
from object_bridge import Fault


class InputAPI:
    def __init__(self): self.window = 10; self.packets = []; self.activations = []; self.hit = 10; self.moves = []
    def foreground(self): return self.window
    def activate(self, window): self.activations.append(window); self.window = window
    def raise_window(self, window): self.activations.append(('reveal',window))
    def modifiers_down(self): return False
    def hit_window(self, x, y): return self.hit
    def move(self, x, y): self.moves.append((x,y))
    def send(self, events): self.packets.append(events)


def descriptor(**changes):
    return dict(window=10,pid=42,enabled=True,offscreen=False,password=False,
                keyboard_focusable=True,keyboard_focused=True,text_input_candidate=True,
                rect={'x':100,'y':200,'width':80,'height':20},**changes)


@pytest.mark.parametrize('method,params', [('press_keys',{'keys':'WIN+R'}),('press_keys',{'keys':'CTRL+A+ENTER'}),
    ('type_text',{'text':'hello\nworld'}),('type_text',{'text':''}),('type_text',{'text':'ok','replace':'yes'}),
    ('click_input',{'button':'middle'}),('scroll_input',{'direction':'forward'}),
    ('scroll_input',{'direction':'up','steps':True}),('scroll_input',{'direction':'up','steps':0}),
    ('scroll_input',{'direction':'down','steps':21})])
def test_invalid_input_has_no_activation_focus_or_packets(method,params):
    api=InputAPI()
    with pytest.raises(Fault) as failure:
        NativeInput(api).execute(method,params,lambda:pytest.fail('must validate before resolving'),lambda:pytest.fail('no focus'))
    assert failure.value.code=='invalid_arguments'
    assert not api.packets and not api.activations


def test_unicode_input_is_literal_without_enter_or_clipboard_and_replace_is_explicit():
    api=InputAPI();focused=[]
    result=NativeInput(api).execute('type_text',{'text':'示例联系人🐟'},descriptor,lambda:focused.append(True))
    assert result['status']=='executed_unverified' and result['trusted_input']
    assert focused==[True]
    assert all(event[0]=='unicode' for packet in api.packets for event in packet)
    units=b''.join(key.to_bytes(2,'little') for packet in api.packets for kind,key,flag in packet if flag==0)
    assert units.decode('utf-16-le')=='示例联系人🐟'


def test_focus_loss_after_partial_text_stops_with_unknown_outcome_and_no_replay():
    api=InputAPI()
    def inspect():
        value=descriptor()
        if api.packets: value['keyboard_focused']=False
        return value
    with pytest.raises(Fault) as failure:
        NativeInput(api).execute('type_text',{'text':'x'*40},inspect,lambda:None)
    assert failure.value.code=='outcome_unknown'
    assert len(api.packets)==1


@pytest.mark.parametrize('change,code', [({'password':True},'protected_control'),({'offscreen':True},'target_unavailable'),
    ({'enabled':False},'target_unavailable'),({'text_input_candidate':False},'unsupported')])
def test_unavailable_or_noneditable_control_never_receives_text(change,code):
    api=InputAPI();target=descriptor();target.update(change)
    with pytest.raises(Fault) as failure:
        NativeInput(api).execute('type_text',{'text':'probe'},lambda:target,lambda:pytest.fail('no focus'))
    assert failure.value.code==code and not api.packets and not api.activations


def test_click_uses_live_bounds_and_rejects_an_occluding_window():
    api=InputAPI();target=descriptor()
    NativeInput(api).execute('click_input',{},lambda:target,lambda:pytest.fail('click does not focus with a provider'))
    assert api.moves==[(140,210)] and len(api.packets)==1
    api=InputAPI();api.hit=99
    with pytest.raises(Fault) as failure: NativeInput(api).execute('click_input',{},lambda:target,lambda:None)
    assert failure.value.code=='target_occluded' and not api.packets


def test_wrong_foreground_before_enter_prevents_message_submission():
    api=InputAPI();target=descriptor()
    def focus(): api.window=99
    with pytest.raises(Fault) as failure: NativeInput(api).execute('press_keys',{'keys':'ENTER'},lambda:target,focus)
    assert failure.value.code=='focus_unverified' and not api.packets


def test_only_an_explicit_bound_click_can_reveal_and_activate_a_background_window():
    api=InputAPI();api.window=99
    def denied(window): raise Fault('focus_unverified','foreground activation denied')
    api.activate=denied
    NativeInput(api).execute('click_input',{},descriptor,lambda:None)
    assert api.activations==[('reveal',10)] and len(api.packets)==1
    api.packets.clear();api.activations.clear()
    with pytest.raises(Fault): NativeInput(api).execute('press_keys',{'keys':'ENTER'},descriptor,lambda:None)
    assert not api.packets and not api.activations


def test_sendinput_partial_failure_is_not_replayed():
    api=InputAPI()
    def fail(events):
        api.packets.append(events)
        raise Fault('outcome_unknown','partial input')
    api.send=fail
    with pytest.raises(Fault) as failure: NativeInput(api).execute('press_keys',{'keys':'ENTER'},descriptor,lambda:None)
    assert failure.value.code=='outcome_unknown' and len(api.packets)==1


def test_windows_input_packet_has_the_native_pointer_aligned_size():
    assert ctypes.sizeof(_Input)==(40 if ctypes.sizeof(ctypes.c_void_p)==8 else 28)


def test_right_click_keeps_move_down_and_up_in_one_bound_batch():
    api=InputAPI()
    result=NativeInput(api).execute('click_input',{'button':'right'},descriptor,lambda:pytest.fail('no keyboard focus'))
    assert result['button']=='right'
    assert api.packets==[[('mouse_move',(140,210),0),('mouse',0,8),('mouse',0,16)]]


@pytest.mark.parametrize('direction,delta,flag',[('up',240,0x800),('down',-240,0x800),('left',-240,0x1000),('right',240,0x1000)])
def test_wheel_is_bound_to_live_geometry_without_requiring_editable_focus(direction,delta,flag):
    api=InputAPI();target=descriptor();target.update(keyboard_focusable=False,keyboard_focused=False,text_input_candidate=False)
    result=NativeInput(api).execute('scroll_input',{'direction':direction,'steps':2},lambda:target,lambda:pytest.fail('wheel does not focus a provider'))
    assert result['direction']==direction and result['steps']==2
    assert api.packets==[[('mouse_move',(140,210),0),('wheel',delta,flag)]]


def test_wheel_rejects_occlusion_and_failed_foreground_without_sending():
    api=InputAPI();api.hit=99
    with pytest.raises(Fault) as failure: NativeInput(api).execute('scroll_input',{'direction':'up'},descriptor,lambda:None)
    assert failure.value.code=='target_occluded' and not api.packets
    api=InputAPI()
    def denied(window): raise Fault('focus_unverified','denied')
    api.activate=denied
    with pytest.raises(Fault): NativeInput(api).execute('scroll_input',{'direction':'up'},descriptor,lambda:None)
    assert not api.packets and not api.activations
