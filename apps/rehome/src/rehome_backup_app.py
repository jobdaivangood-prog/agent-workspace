"""Small, user-started Tk interface for the ReHome22d backup workflow."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import threading
import uuid
import tkinter as tk
from tkinter import filedialog, ttk

import rehome_backup_workflow as workflow
from rehome_worker_lock import WorkerLock


POLL_MS = 100
FALLBACK_ERROR = "Операция не выполнена. Подробности недоступны; проверьте настройки и повторите позже."


def safe_error_text(error):
    """Render only the workflow's deliberately safe error fields."""
    if not isinstance(error, dict):
        return FALLBACK_ERROR
    code, message = error.get("code"), error.get("message")
    if not isinstance(message, str) or not message.strip():
        return FALLBACK_ERROR
    if code is not None and not isinstance(code, str):
        return FALLBACK_ERROR
    return f"{message.strip()} ({code})" if code else message.strip()


class BackupApp:
    def __init__(self, root, settings, *, service=workflow, lock_factory=WorkerLock):
        self.root, self.settings, self.service = root, settings, service
        self.lock_factory = lock_factory
        self.events = queue.Queue()
        self.worker = None
        self.cancel_path = None
        self.lock = None
        self.close_requested = False
        self.settings_path = None
        self.root.title("ReHome — бекап и восстановление Codex")
        self.root.geometry(f'800x{min(820,max(620,self.root.winfo_screenheight()-100))}')
        self.root.minsize(720,620)
        self.root.configure(background='#F3F6FB')
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        style=ttk.Style(root)
        style.theme_use('clam')
        style.configure('.',font=('Segoe UI',10),background='#F3F6FB',foreground='#182A46')
        style.configure('TFrame',background='#F3F6FB')
        style.configure('Card.TFrame',background='#FFFFFF')
        style.configure('Card.TLabel',background='#FFFFFF',foreground='#182A46')
        style.configure('Muted.TLabel',background='#F3F6FB',foreground='#5D6C82')
        style.configure('CardMuted.TLabel',background='#FFFFFF',foreground='#5D6C82')
        style.configure('TButton',padding=(16,10),background='#E5EBF4',borderwidth=0)
        style.map('TButton',background=[('active','#D7E1EF'),('disabled','#E7EBF1')])
        style.configure('Primary.TButton',background='#245CDB',foreground='#FFFFFF',font=('Segoe UI Semibold',11),padding=(18,14))
        style.map('Primary.TButton',background=[('active','#1748BB'),('disabled','#CAD5EB')],foreground=[('disabled','#67758C')])
        style.configure('Secondary.TButton',font=('Segoe UI Semibold',11),padding=(18,14))
        style.configure('Link.TButton',background='#F3F6FB',foreground='#245CDB',padding=(8,5))
        style.configure('Horizontal.TProgressbar',background='#245CDB',troughcolor='#E8EDF5',borderwidth=0)
        icon=Path(__file__).resolve().parent.parent/'Assets/ReHome.ico'
        if icon.is_file():
            try:self.root.iconbitmap(str(icon))
            except tk.TclError:pass
        self.status = tk.StringVar(value="Готово")
        self.context = tk.StringVar(value=self._context_text())
        viewport=tk.Canvas(root,bg='#F3F6FB',highlightthickness=0)
        scrollbar=ttk.Scrollbar(root,orient='vertical',command=viewport.yview)
        scrollbar.pack(side='right',fill='y')
        viewport.pack(side='left',fill='both',expand=True)
        viewport.configure(yscrollcommand=scrollbar.set)
        frame = ttk.Frame(viewport, padding=24)
        content=viewport.create_window((0,0),window=frame,anchor='nw')
        frame.bind('<Configure>',lambda event:viewport.configure(scrollregion=viewport.bbox('all')))
        viewport.bind('<Configure>',lambda event:viewport.itemconfigure(content,width=event.width))
        root.bind('<MouseWheel>',lambda event:viewport.yview_scroll(-int(event.delta/120),'units'))
        header=ttk.Frame(frame);header.pack(fill='x',pady=(0,20))
        logo=tk.Canvas(header,width=52,height=52,bg='#F3F6FB',highlightthickness=0)
        logo.pack(side='left',padx=(0,13))
        logo.create_polygon(5,25,26,6,47,25,42,31,39,28,39,47,13,47,13,28,10,31,fill='#245CDB',outline='')
        logo.create_rectangle(22,31,30,47,fill='#F3F6FB',outline='')
        title=ttk.Frame(header);title.pack(side='left')
        ttk.Label(title,text='ReHome',font=('Segoe UI Semibold',25)).pack(anchor='w')
        ttk.Label(title,text='Ваш Codex — с собой',style='Muted.TLabel').pack(anchor='w')
        ttk.Button(header,text='Инструкция',style='Link.TButton',command=self.open_instructions).pack(side='right',anchor='n',pady=8)
        ttk.Label(frame,text='Бекап и восстановление',font=('Segoe UI Semibold',17)).pack(anchor='w')
        ttk.Label(frame,text='Проекты, переписки и выбранные готовые файлы.',style='Muted.TLabel').pack(anchor='w',pady=(4,18))
        paths=ttk.Frame(frame,padding=16,style='Card.TFrame');paths.pack(fill='x',pady=(0,16))
        ttk.Label(paths,textvariable=self.context,style='Card.TLabel',font=('Segoe UI',9),wraplength=510,justify='left').pack(side='left',anchor='w')
        self.folder_button=ttk.Button(paths,text='Изменить папку копий',command=self.choose_backup_folder)
        self.folder_button.pack(side='right',anchor='center',padx=(12,0))
        actions=ttk.Frame(frame);actions.pack(fill='x')
        self.create_button = ttk.Button(actions, text="Создать бекап", style='Primary.TButton',command=self.start_backup)
        self.restore_button = ttk.Button(actions, text="Восстановить бекап", style='Secondary.TButton',command=self.choose_restore)
        self.create_button.pack(side='left',fill="x",expand=True,pady=3,padx=(0,6))
        self.restore_button.pack(side='left',fill="x",expand=True,pady=3,padx=(6,0))
        self.finished_button = ttk.Button(frame, text="Выбрать готовые файлы", command=self.choose_finished_files)
        self.finished_button.pack(fill="x", pady=(10, 2))
        self.finished_count = tk.StringVar(value=self._finished_count_text())
        ttk.Label(frame, textvariable=self.finished_count).pack(anchor="w")
        ttk.Label(frame, text="Включаются только файлы, которые вы выбрали сами.", style='Muted.TLabel',wraplength=660, justify="left").pack(anchor="w", pady=(2, 8))
        self.recovery_button = ttk.Button(frame, text="Вернуть прежнее состояние", command=self.start_rollback)
        if self._recovery_available():
            self.recovery_button.pack(fill="x", pady=3)
        self.cancel_button = ttk.Button(frame, text="Отменить ожидание", command=self.cancel)
        self.cancel_button.pack(fill="x", pady=(10, 3))
        self.cancel_button.state(["disabled"])
        status_card=ttk.Frame(frame,padding=16,style='Card.TFrame');status_card.pack(fill='both',expand=True,pady=(14,0))
        ttk.Label(status_card,text='СОСТОЯНИЕ',style='CardMuted.TLabel',font=('Segoe UI Semibold',9)).pack(anchor='w',pady=(0,7))
        ttk.Label(status_card, textvariable=self.status, style='Card.TLabel',wraplength=650, justify="left").pack(anchor="w")
        self.process_text = tk.StringVar(value="")
        ttk.Label(status_card, textvariable=self.process_text, style='CardMuted.TLabel',wraplength=650, justify="left").pack(anchor="w", pady=(5, 0))
        self.progress=ttk.Progressbar(status_card,mode='indeterminate')
        self.progress.pack(fill='x',pady=(12,0))
        ttk.Label(frame, text="ReHome попросит закрыть Codex перед операцией. Прежнее состояние сохраняется для отката.",style='Muted.TLabel',wraplength=700, justify="left").pack(anchor="w", pady=(14, 0))
        ttk.Label(frame,text='ReHome 0.1.40 · beta  |  Windows x64',style='Muted.TLabel',font=('Segoe UI',9)).pack(anchor='w',pady=(12,0))
        root.after(POLL_MS, self._poll)

    def _context_text(self):
        return f"Профиль Codex\n{self.settings.get('source_profile',self.settings['local_root'])}\n\nПапка копий\n{self.settings['backup_root']}"

    def open_instructions(self):
        help_file=Path(__file__).resolve().parent.parent/'Assets/Instructions.html'
        if help_file.is_file():os.startfile(help_file)
        else:self.status.set('Инструкция отсутствует в комплекте. Распакуйте полный архив приложения.')

    def choose_backup_folder(self):
        chosen=filedialog.askdirectory(initialdir=self.settings['backup_root'],mustexist=True,title='Папка для бекапов')
        if not chosen:return
        try:
            self.settings=self.service.set_backup_folder(self.settings,chosen,self.settings_path)
            self.context.set(self._context_text())
            self.status.set('Папка копий сохранена.')
        except Exception:
            self.status.set('Не удалось выбрать папку. Используйте отдельную локальную папку или папку Synology Drive.')

    def _recovery_available(self):
        method=getattr(self.service,'recovery_available',None)
        return method(self.settings) if method else (Path(self.settings['local_root'])/'latest-transaction.json').is_file()

    def _set_busy(self, busy):
        for button in (self.create_button, self.restore_button, self.recovery_button, self.finished_button,self.folder_button):
            try:
                button.state(["disabled"] if busy else ["!disabled"])
            except tk.TclError:
                pass
        if busy:self.progress.start(14)
        else:self.progress.stop()
        self.cancel_button.state(["!disabled"] if busy else ["disabled"])

    def _finished_count_text(self):
        return f"Выбрано готовых файлов: {len(self.settings.get('finished_results', []))}"

    def choose_finished_files(self):
        choices_fn = getattr(self.service, "project_choices", None)
        infer = getattr(self.service, "infer_project", None)
        if not callable(choices_fn) or not callable(infer):
            self.status.set("Выбор готовых файлов пока недоступен.")
            return
        try:
            choices = choices_fn(self.settings)
        except Exception:
            self.status.set("Не удалось получить список проектов.")
            return
        if not choices:
            self.status.set("Нет доступных проектов для привязки готовых файлов.")
            return
        paths = filedialog.askopenfilenames(title="Выберите только готовые файлы результата")
        if not paths:
            return
        existing = list(self.settings.get("finished_results", []))
        for raw_path in paths:
            path = Path(raw_path)
            try:
                project_id = infer(str(path), choices)
            except Exception:
                project_id = None
            if not project_id:
                project_id = self._ask_project(path, choices)
            if not project_id or not any(item.get("id") == project_id for item in choices):
                continue
            item = {"project_id": project_id, "path": str(path), "display_name": path.name,
                    "classification": "selected_finished_generated"}
            if item not in existing:
                existing.append(item)
        self.settings["finished_results"] = existing
        self.finished_count.set(self._finished_count_text())

    def _ask_project(self, path, choices):
        dialog = tk.Toplevel(self.root)
        dialog.title("Привязать файл к проекту")
        dialog.transient(self.root)
        dialog.grab_set()
        result = {"project_id": None}
        ttk.Label(dialog, text=f"Файл: {path.name}\nВыберите проект:", wraplength=460, justify="left").pack(padx=16, pady=(16, 8), anchor="w")
        labels = [f"{item.get('name') or item['id']} ({item['id']})" for item in choices]
        selected = tk.StringVar(value=labels[0])
        ttk.Combobox(dialog, textvariable=selected, values=labels, state="readonly", width=58).pack(padx=16, pady=4)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=16, pady=16)
        def accept():
            index = labels.index(selected.get()) if selected.get() in labels else -1
            if index >= 0:
                result["project_id"] = choices[index]["id"]
            dialog.destroy()
        ttk.Button(buttons, text="Выбрать", command=accept).pack(side="left")
        ttk.Button(buttons, text="Пропустить файл", command=dialog.destroy).pack(side="right")
        dialog.wait_window()
        return result["project_id"]

    def start_backup(self):
        self._start("backup")

    def choose_restore(self):
        chosen = filedialog.askdirectory(initialdir=self.settings["backup_root"], mustexist=True)
        if chosen:
            self._start("restore", Path(chosen))

    def start_rollback(self):
        self._start("rollback")

    def _start(self, action, backup_path=None):
        if self.worker and self.worker.is_alive():
            return
        local_root = Path(self.settings["local_root"])
        self.cancel_path = local_root / (".rehome-cancel-" + uuid.uuid4().hex)
        try:
            self.lock = self.lock_factory(local_root / "controller.lock").acquire()
        except Exception:
            self.status.set("Уже выполняется другая операция. Закройте другое окно и повторите запуск.")
            return
        self._set_busy(True)
        self.status.set("Подготовка операции…")
        def run():
            try:
                if action == "backup":
                    result = self.service.create_backup(self.settings, self.cancel_path, self.events)
                elif action == "restore":
                    result = self.service.restore_backup(self.settings, backup_path, self.cancel_path, self.events)
                else:
                    result = self.service.rollback(self.settings, self.cancel_path, self.events)
                self.events.put(("result", result))
            except Exception as error:
                # Never put exception text or tracebacks on the UI channel.
                known=getattr(self.service,'WorkflowError',())
                safe={'code':error.code,'message':error.public_message} if isinstance(error,known) else {
                    'code':'operation_failed','message':'Операция не выполнена. Состояние сохранено; доступен возврат прежних данных.'}
                self.events.put(('error',safe))
            finally:
                try:
                    if self.lock:
                        self.lock.release()
                finally:
                    self.lock = None
                    try:
                        self.cancel_path.unlink()
                    except OSError:
                        pass
        self.worker = threading.Thread(target=run, name="rehome-backup-worker", daemon=True)
        self.worker.start()

    def cancel(self):
        if self.worker and self.worker.is_alive() and self.cancel_path:
            try:
                # Exclusive creation avoids replacing an existing/user file.
                with self.cancel_path.open("x", encoding="utf-8") as stream:
                    stream.write("cancel\n")
                self.status.set("Запрос отмены отправлен. Ожидаю безопасного завершения…")
            except FileExistsError:
                self.status.set("Отмена уже запрошена. Ожидаю завершения…")
            except OSError:
                self.status.set("Не удалось передать отмену. Ожидаю завершения операции…")

    def _poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind, payload = event
                if kind == "progress" and isinstance(payload, str):
                    self.status.set(payload)
                elif kind == "processes" and isinstance(payload, dict):
                    count = payload.get("pending_count", 0)
                    pids = payload.get("pending_pids", [])
                    if isinstance(count, int) and isinstance(pids, (list, tuple)):
                        self.process_text.set(f"Ожидающих процессов Codex: {count}" + (f" (PID: {', '.join(map(str, pids))})" if pids else ""))
                elif kind == "result":
                    self.status.set(self._result_text(payload))
                    if isinstance(payload,dict) and payload.get('rollback_available'):
                        self.recovery_button.pack(fill='x',pady=3,before=self.cancel_button)
                elif kind == "error":
                    self.status.set(safe_error_text(payload))
                if kind in {'result','error'} and self._recovery_available():
                    self.recovery_button.pack(fill='x',pady=3,before=self.cancel_button)
        except queue.Empty:
            pass
        alive = bool(self.worker and self.worker.is_alive())
        if self.worker and not alive:
            self.worker = None
            self._set_busy(False)
            if self.close_requested:
                self.root.destroy()
                return
        self.root.after(POLL_MS, self._poll)

    @staticmethod
    def _result_text(result):
        if not isinstance(result, dict):
            return "Операция завершена."
        message = result.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return "Операция завершена." if result.get("status") in {"ok", "completed", "success"} else "Операция завершена; проверьте итоговый статус в отчёте."

    def close(self):
        if self.worker and self.worker.is_alive():
            self.close_requested = True
            self.cancel()
            self.status.set("Окно закроется после завершения отмены или операции.")
        else:
            self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="ReHome22d backup UI")
    parser.add_argument("--settings", required=True, help="Абсолютный путь к JSON настроек")
    args = parser.parse_args(argv)
    settings_path = Path(args.settings)
    if not settings_path.is_absolute():
        parser.error("--settings должен содержать абсолютный путь")
    settings = workflow.load_settings(settings_path)
    root = tk.Tk()
    app=BackupApp(root, settings)
    app.settings_path=settings_path
    root.mainloop()


if __name__ == "__main__":
    main()
