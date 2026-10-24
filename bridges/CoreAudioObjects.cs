using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace SystemObjectAdapters {
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    class MMDeviceEnumeratorClass { }
    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator {
        [PreserveSig] int EnumAudioEndpoints(int flow, uint mask, out IMMDeviceCollection devices);
        [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice device);
        [PreserveSig] int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice device);
        [PreserveSig] int RegisterEndpointNotificationCallback(IntPtr callback);
        [PreserveSig] int UnregisterEndpointNotificationCallback(IntPtr callback);
    }
    [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceCollection {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int Item(uint index, out IMMDevice device);
    }
    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice {
        [PreserveSig] int Activate(ref Guid iid, uint context, IntPtr parameters, [MarshalAs(UnmanagedType.IUnknown)] out object result);
        [PreserveSig] int OpenPropertyStore(uint access, out IntPtr store);
        [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
        [PreserveSig] int GetState(out uint state);
    }
    [ComImport, Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionManager2 {
        [PreserveSig] int GetAudioSessionControl(ref Guid guid, uint flags, out IAudioSessionControl2 result);
        [PreserveSig] int GetSimpleAudioVolume(ref Guid guid, uint flags, out ISimpleAudioVolume result);
        [PreserveSig] int GetSessionEnumerator(out IAudioSessionEnumerator result);
        [PreserveSig] int RegisterSessionNotification(IntPtr callback);
        [PreserveSig] int UnregisterSessionNotification(IntPtr callback);
        [PreserveSig] int RegisterDuckNotification([MarshalAs(UnmanagedType.LPWStr)] string id, IntPtr callback);
        [PreserveSig] int UnregisterDuckNotification(IntPtr callback);
    }
    [ComImport, Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionEnumerator {
        [PreserveSig] int GetCount(out int count);
        [PreserveSig] int GetSession(int index, [MarshalAs(UnmanagedType.IUnknown)] out object result);
    }
    [ComImport, Guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionControl2 {
        [PreserveSig] int GetState(out int state);
        [PreserveSig] int GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        [PreserveSig] int SetDisplayName([MarshalAs(UnmanagedType.LPWStr)] string name, ref Guid context);
        [PreserveSig] int GetIconPath([MarshalAs(UnmanagedType.LPWStr)] out string path);
        [PreserveSig] int SetIconPath([MarshalAs(UnmanagedType.LPWStr)] string path, ref Guid context);
        [PreserveSig] int GetGroupingParam(out Guid group);
        [PreserveSig] int SetGroupingParam(ref Guid group, ref Guid context);
        [PreserveSig] int RegisterAudioSessionNotification(IntPtr callback);
        [PreserveSig] int UnregisterAudioSessionNotification(IntPtr callback);
        [PreserveSig] int GetSessionIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string id);
        [PreserveSig] int GetSessionInstanceIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string id);
        [PreserveSig] int GetProcessId(out uint pid);
        [PreserveSig] int IsSystemSoundsSession();
        [PreserveSig] int SetDuckingPreference([MarshalAs(UnmanagedType.Bool)] bool optOut);
    }
    [ComImport, Guid("87CE5498-68D6-44E5-9215-6DA47EF883D8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface ISimpleAudioVolume {
        [PreserveSig] int SetMasterVolume(float level, ref Guid context);
        [PreserveSig] int GetMasterVolume(out float level);
        [PreserveSig] int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, ref Guid context);
        [PreserveSig] int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
    }
    public class AudioSessionObject {
        readonly IAudioSessionControl2 control;
        readonly ISimpleAudioVolume volume;
        public int ProcessId { get; private set; }
        public string ProcessName { get; private set; }
        public string Interface { get { return "IAudioSessionControl2 / ISimpleAudioVolume"; } }
        internal AudioSessionObject(object native, uint pid, string name) { control = (IAudioSessionControl2)native; volume = (ISimpleAudioVolume)native; ProcessId = (int)pid; ProcessName = name; }
        public string State { get { int state; Marshal.ThrowExceptionForHR(control.GetState(out state)); return new[] { "inactive", "active", "expired" }[state]; } }
        public float Volume { get { float level; Marshal.ThrowExceptionForHR(volume.GetMasterVolume(out level)); return level; } }
        public bool Muted { get { bool mute; Marshal.ThrowExceptionForHR(volume.GetMute(out mute)); return mute; } }
        public void SetVolume(float level) { if (level < 0 || level > 1 || Single.IsNaN(level)) throw new ArgumentOutOfRangeException("level"); Guid context = Guid.Empty; Marshal.ThrowExceptionForHR(volume.SetMasterVolume(level, ref context)); }
        public void SetMuted(bool mute) { Guid context = Guid.Empty; Marshal.ThrowExceptionForHR(volume.SetMute(mute, ref context)); }
    }
    public static class CoreAudioFactory {
        public static IDictionary<string, object> CreateRoots() {
            var result = new Dictionary<string, object>();
            var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumeratorClass();
            IMMDeviceCollection devices; Marshal.ThrowExceptionForHR(enumerator.EnumAudioEndpoints(0, 1, out devices));
            uint count; Marshal.ThrowExceptionForHR(devices.GetCount(out count));
            for (uint d = 0; d < count && d < 32; d++) {
                IMMDevice device; Marshal.ThrowExceptionForHR(devices.Item(d, out device));
                object nativeManager; Guid iid = typeof(IAudioSessionManager2).GUID;
                if (device.Activate(ref iid, 23, IntPtr.Zero, out nativeManager) < 0) continue;
                var manager = (IAudioSessionManager2)nativeManager;
                IAudioSessionEnumerator sessions; Marshal.ThrowExceptionForHR(manager.GetSessionEnumerator(out sessions));
                int sessionCount; Marshal.ThrowExceptionForHR(sessions.GetCount(out sessionCount));
                for (int i = 0; i < sessionCount && i < 512; i++) {
                    object session; if (sessions.GetSession(i, out session) < 0) continue;
                    var control = (IAudioSessionControl2)session;
                    uint pid; if (control.GetProcessId(out pid) < 0 || pid == 0) continue;
                    string name; try { name = Process.GetProcessById((int)pid).ProcessName; } catch { continue; }
                    // Scope this provider to the four applications under test.
                    if (name != "cloudmusic" && name != "Weixin" && name != "WeChat" && name != "QQ" && name != "wps" && name != "et" && name != "wpp") continue;
                    result[name + ":" + pid + ":" + d + ":" + i] = new AudioSessionObject(session, pid, name);
                }
            }
            return result;
        }
    }
}
