using System;
using System.Drawing;
using System.IO;
using System.Windows.Forms;
using System.Web.Script.Serialization;

public class FixtureForm : Form {
    readonly string StatePath;
    public int Count { get; private set; }
    public string CustomText { get; set; }
    public bool CustomChecked { get; set; }
    public TextBox Standard;
    Label Counter;
    public FixtureForm(string statePath,bool mutableName=false) {
        StatePath=statePath; CustomText=""; Text="AppCtl 通道验证"; Name="AppCtlNativeFixture"; Size=new Size(650,420); ShowInTaskbar=false;
        Standard=new TextBox { Name="StandardEdit",AccessibleName="标准输入框",Location=new Point(20,25),Width=300 };
        Standard.TextChanged+=delegate{ if(mutableName) Standard.AccessibleName=Standard.Text.Length==0 ? "标准输入框" : "已编辑的输入框"; Save(); }; Controls.Add(Standard);
        var button=new Button { Name="Increment",Text="计数增加",AccessibleName="计数增加",Location=new Point(340,20),Size=new Size(120,35) };
        button.Click+=delegate{ Increment(); }; Controls.Add(button);
        var check=new CheckBox { Name="StandardCheck",Text="标准复选框",AccessibleName="标准复选框",Location=new Point(20,65),Width=180 }; Controls.Add(check);
        Counter=new Label { AccessibleName="计数结果",Text="计数=0",Location=new Point(220,70),Width=200 }; Controls.Add(Counter);
        var password=new TextBox { Name="ProtectedEdit",AccessibleName="密码测试框",UseSystemPasswordChar=true,Text="fixture-only",Location=new Point(20,105),Width=300 }; Controls.Add(password);
        var canvas=new CustomCanvas(this) { Name="CustomCanvas",AccessibleName="测试自绘画布",Location=new Point(20,155),Size=new Size(580,190) }; Controls.Add(canvas);
        Shown+=delegate{ Save(); };
    }
    public void Increment() { Count++; Counter.Text="计数="+Count; Save(); }
    public void Save() {
        if(!IsHandleCreated || Counter==null || Standard==null) return;
        var text=new JavaScriptSerializer().Serialize(new { window=Handle.ToInt64(),pid=System.Diagnostics.Process.GetCurrentProcess().Id,count=Count,standard_text=Standard.Text,custom_text=CustomText,custom_checked=CustomChecked });
        File.WriteAllText(StatePath,text,System.Text.Encoding.UTF8);
    }
    [STAThread] public static void Main(string[] args) { Application.EnableVisualStyles(); Application.Run(new FixtureForm(args[0],args.Length>1 && args[1]=="--mutable-name")); }
}
public class CustomCanvas : Control {
    public readonly FixtureForm Form;
    public CustomCanvas(FixtureForm form) { Form=form; SetStyle(ControlStyles.UserPaint|ControlStyles.AllPaintingInWmPaint,true); }
    protected override AccessibleObject CreateAccessibilityInstance() { return new CanvasAccessible(this); }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e); e.Graphics.Clear(Color.WhiteSmoke);
        e.Graphics.DrawString("自绘区域，另外提供 MSAA 对象结构",Font,Brushes.Black,10,10);
        e.Graphics.DrawRectangle(Pens.SteelBlue,10,45,160,35); e.Graphics.DrawString("自绘计数增加",Font,Brushes.Black,20,55);
        e.Graphics.DrawRectangle(Pens.SteelBlue,10,95,350,35); e.Graphics.DrawString("自绘输入槽："+Form.CustomText,Font,Brushes.Black,20,105);
        e.Graphics.DrawString((Form.CustomChecked?"[✓]":"[ ]")+" 自绘复选框",Font,Brushes.Black,20,150);
    }
    public void Changed(int child) { Invalidate(); AccessibilityNotifyClients(AccessibleEvents.ValueChange,child); Form.Save(); }
}
public class CanvasAccessible : Control.ControlAccessibleObject {
    readonly CustomCanvas Canvas;
    readonly AccessibleObject[] Children;
    public CanvasAccessible(CustomCanvas c):base(c) { Canvas=c; Children=new AccessibleObject[]{new SlotAccessible(this,c,0),new SlotAccessible(this,c,1),new SlotAccessible(this,c,2)}; }
    public override AccessibleRole Role { get { return AccessibleRole.Pane; } }
    public override int GetChildCount() { return Children.Length; }
    public override AccessibleObject GetChild(int index) { return index>=0 && index<Children.Length ? Children[index] : null; }
}
public class SlotAccessible : AccessibleObject {
    readonly CanvasAccessible ParentObject;
    readonly CustomCanvas Canvas;
    readonly int Index;
    public SlotAccessible(CanvasAccessible p,CustomCanvas c,int index) { ParentObject=p; Canvas=c; Index=index; }
    public override AccessibleObject Parent { get { return ParentObject; } }
    public override string Name { get { return Index==0 ? "自绘计数增加" : Index==1 ? "自绘输入槽" : "自绘复选框"; } set {} }
    public override AccessibleRole Role { get { return Index==0 ? AccessibleRole.PushButton : Index==1 ? AccessibleRole.Text : AccessibleRole.CheckButton; } }
    public override AccessibleStates State { get { return AccessibleStates.Focusable | (Index==2 && Canvas.Form.CustomChecked ? AccessibleStates.Checked : AccessibleStates.None); } }
    public override Rectangle Bounds { get { return Canvas.RectangleToScreen(Index==0 ? new Rectangle(10,45,160,35) : Index==1 ? new Rectangle(10,95,350,35) : new Rectangle(10,140,220,35)); } }
    public override string DefaultAction { get { return Index==0 ? "Press" : Index==2 ? "Toggle" : ""; } }
    public override string Value {
        get { return Index==1 ? Canvas.Form.CustomText : Index==0 ? Canvas.Form.Count.ToString() : Canvas.Form.CustomChecked.ToString(); }
        set { if(Index==1) { Canvas.Form.CustomText=value; Canvas.Changed(Index); } }
    }
    public override void DoDefaultAction() {
        if(Index==0) Canvas.Form.Increment();
        if(Index==2) { Canvas.Form.CustomChecked=!Canvas.Form.CustomChecked; Canvas.Changed(Index); }
    }
}
