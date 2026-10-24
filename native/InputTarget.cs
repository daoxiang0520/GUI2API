using System.Collections.Generic;

namespace AppCtl {
    public static partial class Worker {
        static object InputTarget(long window,Dictionary<string,object> rect,string role,bool enabled,bool offscreen,bool password,bool focusable,bool focused,bool readOnly,string name="") {
            bool text=(role=="Edit" || role=="ComboBox" || role=="Document" || role=="Group" || role=="Custom") && focusable && !readOnly;
            return D("window",window,"pid",Pid(window),"rect",rect,"role",role,"name",name,"enabled",enabled,"offscreen",offscreen,
                "password",password,"keyboard_focusable",focusable,"keyboard_focused",focused,"text_input_candidate",text);
        }
    }
}
