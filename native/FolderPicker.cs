// Build-time C# 5 / .NET Framework adapter; the runtime uses the Windows dialog.
// No file contents, settings, network APIs or command-line scripts are accessed.
// https://learn.microsoft.com/en-us/windows/win32/shell/common-file-dialog
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class FolderPicker
{
    private const uint FOS_NOCHANGEDIR = 0x00000008;
    private const uint FOS_PICKFOLDERS = 0x00000020;
    private const uint FOS_FORCEFILESYSTEM = 0x00000040;
    private const uint FOS_PATHMUSTEXIST = 0x00000800;
    private const uint FOS_DONTADDTORECENT = 0x02000000;
    private const uint SIGDN_FILESYSPATH = 0x80058000;
    private const int Cancelled = unchecked((int)0x800704C7);

    [STAThread]
    private static int Main()
    {
        IFileDialog dialog = null;
        IShellItem item = null;
        IntPtr pathBuffer = IntPtr.Zero;
        try
        {
            Type dialogType = Type.GetTypeFromCLSID(new Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7"), true);
            dialog = (IFileDialog)Activator.CreateInstance(dialogType);
            uint options;
            dialog.GetOptions(out options);
            dialog.SetOptions(options | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR | FOS_DONTADDTORECENT);
            dialog.SetTitle("Agent Workspace — папка общей базы");
            dialog.SetOkButtonLabel("Выбрать папку");
            int result = dialog.Show(IntPtr.Zero);
            if (result == Cancelled) return 2;
            Marshal.ThrowExceptionForHR(result);
            dialog.GetResult(out item);
            item.GetDisplayName(SIGDN_FILESYSPATH, out pathBuffer);
            string selected = Marshal.PtrToStringUni(pathBuffer);
            if (String.IsNullOrEmpty(selected)) throw new InvalidOperationException("No filesystem folder selected.");
            using (StreamWriter output = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false))) output.WriteLine(selected);
            return 0;
        }
        catch (Exception error)
        {
            using (StreamWriter output = new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false))) output.WriteLine("Folder selection failed: " + error.Message);
            return 1;
        }
        finally
        {
            if (pathBuffer != IntPtr.Zero) Marshal.FreeCoTaskMem(pathBuffer);
            if (item != null && Marshal.IsComObject(item)) Marshal.FinalReleaseComObject(item);
            if (dialog != null && Marshal.IsComObject(dialog)) Marshal.FinalReleaseComObject(dialog);
        }
    }

    // Method order follows the Windows SDK IModalWindow/IFileDialog vtable.
    [ComImport, Guid("42F85136-DB7E-439C-85F1-E4075D135FC8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileDialog
    {
        [PreserveSig] int Show(IntPtr owner);
        void SetFileTypes(uint count, IntPtr filters);
        void SetFileTypeIndex(uint index);
        void GetFileTypeIndex(out uint index);
        void Advise(IntPtr events, out uint cookie);
        void Unadvise(uint cookie);
        void SetOptions(uint options);
        void GetOptions(out uint options);
        void SetDefaultFolder([MarshalAs(UnmanagedType.Interface)] IShellItem folder);
        void SetFolder([MarshalAs(UnmanagedType.Interface)] IShellItem folder);
        void GetFolder([MarshalAs(UnmanagedType.Interface)] out IShellItem folder);
        void GetCurrentSelection([MarshalAs(UnmanagedType.Interface)] out IShellItem item);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetFileName(out IntPtr name);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetResult([MarshalAs(UnmanagedType.Interface)] out IShellItem item);
        void AddPlace([MarshalAs(UnmanagedType.Interface)] IShellItem item, uint placement);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string extension);
        void Close(int result);
        void SetClientGuid(ref Guid client);
        void ClearClientData();
        void SetFilter(IntPtr filter);
    }

    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem
    {
        void BindToHandler(IntPtr context, ref Guid handler, ref Guid requestedInterface, out IntPtr result);
        void GetParent([MarshalAs(UnmanagedType.Interface)] out IShellItem parent);
        void GetDisplayName(uint format, out IntPtr name);
        void GetAttributes(uint mask, out uint attributes);
        void Compare([MarshalAs(UnmanagedType.Interface)] IShellItem other, uint hint, out int order);
    }
}
