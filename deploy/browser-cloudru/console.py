"""Graphical handoff on the VM display, inside the owner's Cloud.ru console."""
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import controller as c

FIELDS = [
    ("project", "ID проекта Cloud.ru"), ("key_id", "Key ID установщика"),
    ("secret", "Key Secret установщика"), ("bucket", "Имя закрытого бакета"),
    ("s3_key", "S3 Access Key (tenant_id:key_id)"), ("s3_secret", "S3 Secret Key"),
]


class Wizard:
    def __init__(self, window, state):
        self.window = window
        self.state = state
        self.events = queue.Queue()
        self.busy = False
        window.title("Portable Context")
        self.frame = ttk.Frame(window, padding=16)
        self.frame.pack(expand=True, fill="both")
        self.status = tk.StringVar()
        ttk.Label(window, textvariable=self.status, wraplength=600).pack(pady=4)
        self.home()
        window.after(250, self.poll)

    def clear(self):
        for child in self.frame.winfo_children():
            child.destroy()

    def label(self, message):
        ttk.Label(self.frame, text=message, wraplength=600, justify="left").pack(anchor="w", pady=3)

    def button(self, label, action):
        ttk.Button(self.frame, text=label, command=action).pack(anchor="w", pady=4)

    def password(self, prompt="Введите сохранённый пароль резервных копий."):
        return simpledialog.askstring("Подтверждение владельца", prompt, show="*", parent=self.window)

    def home(self):
        self.clear()
        self.label("Portable Context · установка в вашем проекте")
        s = self.state
        if (s / "https-installed.json").exists():
            self.label(c.setup.read(s / "https-installed.json")["mcp_url"])
            self.button("Получить код для регистрации passkey", self.enroll)
            self.button("Сохранить зашифрованный комплект восстановления в S3", self.export)
        elif (s / "https-plan.json").exists():
            self.show_plan(c.setup.read(s / "https-plan.json"))
        elif (s / "browser-prepared.json").exists():
            self.button("Проверить проект и подготовить план", self.review)
            self.button("Исправить ключи доступа", self.edit)
        else:
            self.label("Откройте локальный файл, которым вы подготовили cloud-init. "
                       "Введите сохранённый пароль, чтобы расшифровать настройки на этой ВМ.")
            self.button("Расшифровать и проверить настройки", self.start)
            self.button("Исправить исходные настройки", self.edit)

    def start(self):
        pw = self.password("Введите пароль из локального помощника.")
        if pw:
            def prepare():
                fields = c.unseal(Path("/etc/pc-browser-input.enc").read_bytes(), pw)
                c.initialize(self.state, fields, pw)
            self.work("Проверка S3", prepare, lambda _: self.review())

    def edit(self):
        pw = self.password("Введите пароль из локального помощника для исправления данных.")
        if pw:
            self.work("Проверка пароля", lambda: c.unseal(Path("/etc/pc-browser-input.enc").read_bytes(), pw),
                      lambda fields: self.form(fields, pw))

    def form(self, fields, pw):
        self.clear()
        self.label("Укажите ключи из кабинета Cloud.ru. Они сохраняются на этой ВМ.")
        inputs = {}
        for key, label in FIELDS:
            self.label(label)
            entry = ttk.Entry(self.frame, width=58, show="*" if "secret" in key or "key" in key else "")
            entry.pack(anchor="w")
            entry.insert(0, fields[key])
            inputs[key] = entry

        def submit():
            values = {key: entry.get() for key, entry in inputs.items()}
            self.work("Проверка бакета и ключей", lambda: c.initialize(self.state, values, pw), lambda _: self.review())

        self.button("Проверить настройки и показать план", submit)

    def review(self):
        self.work("Чтение инфраструктуры", lambda: c.review(self.state, self.progress), self.show_plan)

    def show_plan(self, plan):
        self.clear()
        config = plan["config"]
        self.label("План установки HTTPS")
        self.label(f"Проект {config['project_id']}\nПубличный IPv4 ВМ {config['origin_ip']}\n"
                   f"Выделенная группа {config['security_group_id']}\nИмя контейнера {config['container_name']}")
        self.label("Используются существующие ВМ и бакет. Установщик настроит зашифрованные копии памяти и OAuth, "
                   "закрытый Artifact Registry, Container Apps (0–1 экземпляр), Secret Management и входящий TCP 9443 с mTLS.")
        self.label("Продолжается оплата ВМ, диска и IPv4. Добавляются хранение в S3 и реестре, секреты, "
                   "работа контейнера и трафик. Сверьте текущие цены и остатки квот в кабинете Cloud.ru.")
        def approve():
            pw = self.password()
            if pw is None:
                return
            try:
                c.unlock(self.state, pw)
            except ValueError:
                self.status.set("Неверный пароль.")
                return
            if messagebox.askyesno("Подтвердить расходы", "Тарифы и квоты проверены. Применить показанный план в этом проекте?"):
                self.work("Установка HTTPS", lambda: c.install(self.state, plan["plan_sha256"]), lambda _: self.home())
        self.button("Подтвердить расходы и продолжить", approve)

    def enroll(self):
        pw = self.password()
        if pw:
            self.work("Получение одноразового кода", lambda: c.login(self.state, pw), self.show_code)

    def show_code(self, result):
        url, login, code = result
        self.home()
        win = tk.Toplevel(self.window)
        win.title("Первый вход")
        ttk.Label(win, text=f"Откройте на своём устройстве\n{login}\n\nКод (20 минут): {code}\n"
                            f"Зарегистрируйте passkey. Адрес для ГигаАгента:\n{url}", padding=12).pack()
        ttk.Button(win, text="Скрыть", command=win.destroy).pack()
        win.after(60000, win.destroy)

    def export(self):
        pw = self.password()
        if pw:
            from kit import export
            self.work("Сохранение зашифрованного комплекта", lambda: export(self.state, pw),
                      lambda name: messagebox.showinfo("Объект в вашем бакете", name))

    def progress(self, message):
        self.events.put(("progress", message))

    def work(self, label, job, done):
        if self.busy:
            return
        self.busy = True
        self.clear()
        self.label("Дождитесь завершения. При прерывании откройте консоль этой же ВМ.")
        self.status.set(label)
        self.done = done
        def run():
            try:
                self.events.put(("done", job()))
            except Exception as error:
                # Provider exceptions may include secrets. Only explicit UI errors can be shown.
                self.events.put(("error", str(error) if isinstance(error, c.InputError) else
                                 f"{label}: {type(error).__name__}. Проверьте проект, права и сеть. "
                                 "Ресурсы сохранены."))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "progress":
                    self.status.set(value)
                else:
                    self.busy = False
                    if event == "done":
                        self.status.set("")
                        self.done(value)
                    else:
                        self.home()
                        self.status.set(value)
        except queue.Empty:
            pass
        if self.busy and (self.state / "https-journal.json").exists():
            stage = c.setup.read(self.state / "https-journal.json").get("stage", "")
            self.status.set(f"Этап HTTPS: {stage}. Ресурсы тарифицируются во время ожидания.")
        self.window.after(250, self.poll)


if __name__ == "__main__":
    os.umask(0o077)
    state = c.setup.private_directory(Path.home() / ".local/share/pc-browser-state")
    window = tk.Tk()
    Wizard(window, state)
    window.mainloop()
