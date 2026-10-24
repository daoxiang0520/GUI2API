using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace AppCtl {
    public static partial class Worker {
        [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint rights,bool inherit,int pid);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("advapi32.dll",SetLastError=true)] static extern bool OpenProcessToken(IntPtr process,uint rights,out IntPtr token);
        [DllImport("advapi32.dll",SetLastError=true)] static extern bool GetTokenInformation(IntPtr token,int kind,IntPtr buffer,int size,out int needed);
        [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
        [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthority(IntPtr sid,uint index);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool QueryFullProcessImageName(IntPtr process,int flags,StringBuilder text,ref int length);
        [DllImport("kernel32.dll")] static extern bool ProcessIdToSessionId(uint pid,out uint session);
        [DllImport("wtsapi32.dll",SetLastError=true)] static extern bool WTSQuerySessionInformationW(IntPtr server,uint session,int info,out IntPtr buffer,out int bytes);
        [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr buffer);
        [DllImport("user32.dll",SetLastError=true)] static extern IntPtr OpenInputDesktop(uint flags,bool inherit,uint rights);
        [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr desktop);
        [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool GetUserObjectInformationW(IntPtr obj,int kind,StringBuilder value,int size,out int needed);
        static Dictionary<string,object> DesktopContext() {
            uint session; ProcessIdToSessionId((uint)Process.GetCurrentProcess().Id,out session);
            IntPtr buffer=IntPtr.Zero; int bytes; object locked=null; int flags=-1;
            if(WTSQuerySessionInformationW(IntPtr.Zero,session,25,out buffer,out bytes)) {
                try { if(bytes>=20 && Marshal.ReadInt32(buffer)==1 && Marshal.ReadInt32(buffer,8)==session) { flags=Marshal.ReadInt32(buffer,16); if(flags==0 || flags==1) locked=(flags==0); } } finally { WTSFreeMemory(buffer); }
            }
            string name=""; int inputError=0; IntPtr desktop=OpenInputDesktop(0,false,1);
            if(desktop!=IntPtr.Zero) { try { var text=new StringBuilder(256); int needed; if(GetUserObjectInformationW(desktop,2,text,text.Capacity*2,out needed)) name=text.ToString(); } finally { CloseDesktop(desktop); } }
            else inputError=Marshal.GetLastWin32Error();
            return D("session_id",session,"session_locked",locked,"session_flags",flags,"input_desktop",name,"input_desktop_error",inputError,"interactive_confirmed",locked is bool && !(bool)locked && name=="Default");
        }
        static string IntegrityName(int rid) { return rid>=0x4000 ? "system" : rid>=0x3000 ? "high" : rid>0x2000 ? "medium_plus" : rid>=0x2000 ? "medium" : rid>=0x1000 ? "low" : "untrusted"; }
        static Dictionary<string,object> ProcessIntegrity(int pid) {
            IntPtr process=OpenProcess(0x1000,false,pid),token=IntPtr.Zero,buffer=IntPtr.Zero;
            if(process==IntPtr.Zero) return D("available",false,"win32_error",Marshal.GetLastWin32Error());
            try {
                if(!OpenProcessToken(process,8,out token)) return D("available",false,"win32_error",Marshal.GetLastWin32Error());
                int size; GetTokenInformation(token,25,IntPtr.Zero,0,out size);
                if(size<1 || size>4096) return D("available",false,"reason","Invalid token information size.");
                buffer=Marshal.AllocHGlobal(Math.Max(size,4));
                if(!GetTokenInformation(token,25,buffer,size,out size)) return D("available",false,"win32_error",Marshal.GetLastWin32Error());
                IntPtr sid=Marshal.ReadIntPtr(buffer); byte count=Marshal.ReadByte(GetSidSubAuthorityCount(sid));
                if(count<1 || count>15) return D("available",false,"reason","Invalid integrity SID.");
                int rid=Marshal.ReadInt32(GetSidSubAuthority(sid,(uint)(count-1)));
                bool uiAccess=false,elevated=false;
                if(GetTokenInformation(token,26,buffer,4,out size)) uiAccess=Marshal.ReadInt32(buffer)!=0;
                if(GetTokenInformation(token,20,buffer,4,out size)) elevated=Marshal.ReadInt32(buffer)!=0;
                uint session; ProcessIdToSessionId((uint)pid,out session);
                return D("available",true,"integrity",IntegrityName(rid),"integrity_rid",rid,"elevated",elevated,"ui_access",uiAccess,"session_id",session);
            } finally { if(buffer!=IntPtr.Zero) Marshal.FreeHGlobal(buffer); if(token!=IntPtr.Zero) CloseHandle(token); CloseHandle(process); }
        }
        static string LimitedImageName(int pid) {
            IntPtr process=OpenProcess(0x1000,false,pid); if(process==IntPtr.Zero) return "";
            try { int size=32768; var text=new StringBuilder(size); return QueryFullProcessImageName(process,0,text,ref size) ? text.ToString() : ""; } finally { CloseHandle(process); }
        }
        static object AccessContext(int pid) {
            var observer=ProcessIntegrity(Process.GetCurrentProcess().Id); var target=ProcessIntegrity(pid);
            bool known=Convert.ToBoolean(observer["available"]) && Convert.ToBoolean(target["available"]);
            bool mismatch=known && Convert.ToInt32(observer["integrity_rid"])<Convert.ToInt32(target["integrity_rid"]) && !Convert.ToBoolean(observer["ui_access"]);
            return D("observer",observer,"target",target,"privilege_mismatch",known ? (object)mismatch : null,"diagnosis",!known ? "One or both token levels could not be queried; privilege compatibility is unknown." : mismatch ? "UIPI may restrict cross-integrity UI access. Run an explicitly authorized client at a matching level or use a properly installed accessibility client. Do not classify this as an app-specific lack of controls." : "No higher target integrity was detected; this does not prove that the provider is accessible.");
        }
    }
}
