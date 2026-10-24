param([switch]$Describe)
# Generate managed COM declarations from this Windows installation's own UIA
# type library. No package download or application injection is involved.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$target = Join-Path $PSScriptRoot 'Interop.UIAutomationClient.dll'
if (-not [IO.File]::Exists($target)) {
    Add-Type -TypeDefinition @'
using System;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public class AppCtlTypeLibSink : ITypeLibImporterNotifySink {
    public void ReportEvent(ImporterEventKind kind,int code,string message) { }
    public Assembly ResolveRef(object typeLib) { throw new InvalidOperationException("Unexpected UIA type library dependency."); }
}
public static class AppCtlTypeLib {
    [DllImport("oleaut32.dll",CharSet=CharSet.Unicode,PreserveSig=false)]
    static extern void LoadTypeLibEx(string path,int kind,out ITypeLib library);
    public static void Build(string source,string destination) {
        ITypeLib library; LoadTypeLibEx(source,2,out library);
        try {
            var converter=new TypeLibConverter();
            var builder=converter.ConvertTypeLibToAssembly(library,destination,TypeLibImporterFlags.SafeArrayAsSystemArray,new AppCtlTypeLibSink(),null,null,null,null);
            builder.Save(System.IO.Path.GetFileName(destination));
        } finally { Marshal.ReleaseComObject(library); }
    }
}
'@
    [AppCtlTypeLib]::Build((Join-Path $env:SystemRoot 'System32\UIAutomationCore.dll'),$target)
}
if (-not $Describe) { return }
$assembly=[Reflection.Assembly]::LoadFrom($target)
$types=@('IUIAutomation','IUIAutomation2','IUIAutomationElement','IUIAutomationCacheRequest','IUIAutomationTreeWalker','IUIAutomationLegacyIAccessiblePattern')
foreach($name in $types) {
    $type=$assembly.GetTypes() | Where-Object Name -EQ $name
    if ($null -ne $type) { [Console]::WriteLine($type.FullName); foreach($method in $type.GetMethods()) { [Console]::WriteLine($method.ToString()) } }
}
