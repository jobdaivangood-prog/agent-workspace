"""Local password dialog. Passwords stay in process memory, never in arguments or files."""


class PasswordCancelled(Exception):
    pass


def validation_error(password, repeated=None):
    if len(password) < 12:
        return 'Пароль должен содержать минимум 12 символов.'
    if repeated is not None and password != repeated:
        return 'Пароли не совпадают. Проверьте раскладку и Caps Lock или включите показ пароля.'
    return ''


def ask_password_window(confirm):
    import tkinter as tk
    from tkinter import ttk

    try:
        root = tk.Tk()
    except tk.TclError:
        raise RuntimeError('Не удалось открыть окно пароля. Для ввода в терминале запустите start.ps1 с параметром -TerminalPassword.') from None
    result = None
    root.title('Бекап Codex — пароль' if confirm else 'Восстановление Codex — пароль')
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=20)
    frame.grid(sticky='nsew')
    ttk.Label(frame, text='Придумайте пароль для нового файла секретов.' if confirm else 'Введите пароль, заданный при создании файла секретов.', wraplength=460).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
    ttk.Label(frame, text='Пароль (минимум 12 символов)').grid(row=1, column=0, columnspan=2, sticky='w')
    first = ttk.Entry(frame, width=55, show='*')
    first.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(4, 12))
    second = None
    if confirm:
        ttk.Label(frame, text='Повторите пароль').grid(row=3, column=0, columnspan=2, sticky='w')
        second = ttk.Entry(frame, width=55, show='*')
        second.grid(row=4, column=0, columnspan=2, sticky='ew', pady=(4, 12))
    visible = tk.BooleanVar(root, False)

    def toggle():
        for entry in (first, second):
            if entry is not None:
                entry.configure(show='' if visible.get() else '*')

    ttk.Checkbutton(frame, text='Показать пароль', variable=visible, command=toggle).grid(row=5, column=0, columnspan=2, sticky='w')
    error = ttk.Label(frame, text='', foreground='#b42318', wraplength=460)
    error.grid(row=6, column=0, columnspan=2, sticky='w', pady=(12, 8))
    ttk.Label(frame, text='Пароль не сохраняется. Для восстановления он потребуется снова.', wraplength=460).grid(row=7, column=0, columnspan=2, sticky='w', pady=(0, 16))

    def submit(event=None):
        nonlocal result
        password = first.get()
        message = validation_error(password, second.get() if second is not None else None)
        if message:
            error.configure(text=message)
            (second if second is not None and len(password) >= 12 else first).focus_set()
            return
        result = password
        root.quit()

    def cancel(event=None):
        root.quit()

    ttk.Button(frame, text='Отмена', command=cancel).grid(row=8, column=0, sticky='w')
    ttk.Button(frame, text='Создать бекап' if confirm else 'Продолжить', command=submit).grid(row=8, column=1, sticky='e')
    root.protocol('WM_DELETE_WINDOW', cancel)
    root.bind('<Return>', submit)
    root.bind('<Escape>', cancel)
    root.lift()
    root.attributes('-topmost', True)
    root.after(300, lambda: root.attributes('-topmost', False))
    first.focus_set()
    try:
        root.mainloop()
    finally:
        first.delete(0, 'end')
        if second is not None:
            second.delete(0, 'end')
        root.destroy()
    if result is None:
        raise PasswordCancelled('Ввод пароля отменён. Операция не запускалась.')
    return result
