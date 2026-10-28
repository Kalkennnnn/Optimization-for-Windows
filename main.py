
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import winreg
except ImportError:  # не Windows
    winreg = None
try:
    import psutil
except ImportError:
    psutil = None

APP_NAME = "PC Optimizer"
BACKUP_FILE = Path(os.environ.get("APPDATA", Path.home())) / "PCOptimizer" / "backup.json"
HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


@dataclass
class RegCheck:
    id: str
    name: str
    category: str
    description: str
    hive: str            # "HKCU" | "HKLM"
    path: str
    value_name: str
    recommended: int
    admin: bool = False
    reboot: bool = False
    default_on: bool = True 

    def _hive(self):
        return winreg.HKEY_CURRENT_USER if self.hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE

    def read(self):
        try:
            with winreg.OpenKey(self._hive(), self.path, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                return winreg.QueryValueEx(k, self.value_name)[0]
        except OSError:
            return None

    def write(self, value):
        """value=None -> удалить параметр (вернуть к «не задано»)."""
        access = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
        if value is None:
            try:
                with winreg.OpenKey(self._hive(), self.path, 0, access) as k:
                    winreg.DeleteValue(k, self.value_name)
            except FileNotFoundError:
                pass
            return
        with winreg.CreateKeyEx(self._hive(), self.path, 0, access) as k:
            winreg.SetValueEx(k, self.value_name, 0, winreg.REG_DWORD, int(value))

    def is_optimal(self):
        return self.read() == self.recommended

    def display(self, v):
        return "не задано" if v is None else str(v)


class PowerCheck:
    id = "power_plan"
    name = "Схема электропитания"
    category = "Питание"
    description = ("Схема «Высокая производительность» убирает агрессивное снижение частот CPU. "
                   "На ноутбуке сильнее расходует батарею и греется — по умолчанию не отмечено.")
    recommended = HIGH_PERF
    admin = False
    reboot = False
    default_on = False
    _name = ""

    def read(self):
        try:
            out = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True,
                                 text=True, creationflags=NO_WINDOW,
                                 encoding="cp866", errors="ignore").stdout
            m = re.search(r"([0-9a-fA-F-]{36})\s*\(([^)]*)\)", out)
            if m:
                self._name = m.group(2)
                return m.group(1).lower()
        except Exception:
            pass
        return None

    def write(self, value):
        subprocess.run(["powercfg", "/setactive", value], check=True,
                       creationflags=NO_WINDOW, capture_output=True)

    def is_optimal(self):
        return self.read() in (HIGH_PERF, ULTIMATE)

    def display(self, v):
        if v is None:
            return "неизвестно"
        if v == HIGH_PERF:
            return "Высокая производительность"
        return self._name or v


GS = r"Software\Microsoft\Windows\CurrentVersion"
CHECKS = [
    RegCheck("game_mode", "Игровой режим (Game Mode)", "Игры",
             "Windows приоритизирует игру и не запускает фоновые обновления драйверов во время игры.",
             "HKCU", r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1),
    RegCheck("game_dvr", "Фоновая запись Game DVR", "Игры",
             "Фоновая запись геймплея создаёт нагрузку на GPU и диск. Отключите, если не используете.",
             "HKCU", r"System\GameConfigStore", "GameDVR_Enabled", 0),
    RegCheck("app_capture", "Захват приложений (Game Bar)", "Игры",
             "Связанный с Game DVR параметр захвата экрана и клипов.",
             "HKCU", GS + r"\GameDVR", "AppCaptureEnabled", 0),
    RegCheck("hags", "Аппаратное планирование GPU (HAGS)", "Игры",
             "Снижает задержки и нагрузку на CPU на современных GPU (NVIDIA GTX 10+/AMD RX 5000+). "
             "На старых видеокартах может вызывать артефакты. Нужны права админа и перезагрузка.",
             "HKLM", r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", 2,
             admin=True, reboot=True),
    RegCheck("background_apps", "Фоновые приложения", "Система",
             "Запрещает UWP-приложениям работать в фоне: экономит RAM, CPU и батарею.",
             "HKCU", GS + r"\BackgroundAccessApplications", "GlobalUserDisabled", 1),
    RegCheck("transparency", "Эффекты прозрачности", "Визуал",
             "Прозрачность панели и меню слегка нагружает GPU. Заметно только на слабых ПК.",
             "HKCU", GS + r"\Themes\Personalize", "EnableTransparency", 0, default_on=False),
    RegCheck("visual_fx", "Визуальные эффекты (режим скорости)", "Визуал",
             "VisualFXSetting=2 — «Обеспечить наилучшее быстродействие». Отключает анимации и тени. "
             "Меняет внешний вид системы, поэтому по умолчанию не отмечено.",
             "HKCU", GS + r"\Explorer\VisualEffects", "VisualFXSetting", 2, default_on=False),
    RegCheck("tips_suggestions", "Рекомендации и подсказки Windows", "Система",
             "Отключает показ «советов» и рекламных рекомендаций в системе.",
             "HKCU", GS + r"\ContentDeliveryManager", "SubscribedContent-338389Enabled", 0),
    PowerCheck(),
]


class Backup:
    """Хранит ОРИГИНАЛЬНЫЕ значения (до первого применения) для отката."""

    def __init__(self):
        self.data = {}
        try:
            self.data = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    def remember(self, check):
        if check.id in self.data:   # оригинал уже сохранён
            return
        v = check.read()
        self.data[check.id] = {"existed": v is not None, "value": v}
        self.save()

    def save(self):
        BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_FILE.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


# ───────────────────────────── GUI ─────────────────────────────
class App(tk.Tk):
    BG, PANEL, FG, MUTED = "#1e1f26", "#272933", "#e8e9ee", "#9a9db0"
    OK, WARN, ACCENT = "#4cc38a", "#f0b44c", "#5b8def"

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("980x680")
        self.minsize(860, 600)
        self.configure(bg=self.BG)
        self.backup = Backup()
        self.selected = set()
        self.by_id = {c.id: c for c in CHECKS}
        self._style()
        self._build()
        self.scan()
        self._tick()

    # --- оформление
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=self.BG, foreground=self.FG, fieldbackground=self.PANEL)
        s.configure("Treeview", background=self.PANEL, fieldbackground=self.PANEL,
                    foreground=self.FG, rowheight=30, borderwidth=0)
        s.configure("Treeview.Heading", background=self.BG, foreground=self.MUTED,
                    relief="flat", font=("Segoe UI", 9, "bold"))
        s.map("Treeview", background=[("selected", "#343a55")])
        s.configure("TButton", background=self.PANEL, foreground=self.FG, padding=8,
                    borderwidth=0, font=("Segoe UI", 10))
        s.map("TButton", background=[("active", "#33364a")])
        s.configure("Accent.TButton", background=self.ACCENT, foreground="white")
        s.map("Accent.TButton", background=[("active", "#4a78d6")])
        s.configure("Horizontal.TProgressbar", troughcolor=self.PANEL,
                    background=self.ACCENT, borderwidth=0, thickness=8)

    def _build(self):
        top = tk.Frame(self, bg=self.BG)
        top.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(top, text=APP_NAME, bg=self.BG, fg=self.FG,
                 font=("Segoe UI", 17, "bold")).pack(side="left")
        adm = is_admin()
        self.admin_lbl = tk.Label(
            top, bg=self.BG, font=("Segoe UI", 9),
            text="● Администратор" if adm else "● Обычные права (HKLM-параметры недоступны)",
            fg=self.OK if adm else self.WARN)
        self.admin_lbl.pack(side="left", padx=14)
        if not adm:
            ttk.Button(top, text="Перезапустить от админа",
                       command=self.relaunch_admin).pack(side="right")

        # мониторинг
        mon = tk.Frame(self, bg=self.PANEL)
        mon.pack(fill="x", padx=16, pady=6)
        self.cpu_var, self.ram_var, self.disk_var = (tk.DoubleVar() for _ in range(3))
        self.cpu_txt, self.ram_txt, self.disk_txt = (tk.StringVar(value="—") for _ in range(3))
        for i, (label, var, txt) in enumerate((("CPU", self.cpu_var, self.cpu_txt),
                                               ("RAM", self.ram_var, self.ram_txt),
                                               ("Диск C:", self.disk_var, self.disk_txt))):
            f = tk.Frame(mon, bg=self.PANEL)
            f.pack(side="left", expand=True, fill="x", padx=14, pady=10)
            head = tk.Frame(f, bg=self.PANEL)
            head.pack(fill="x")
            tk.Label(head, text=label, bg=self.PANEL, fg=self.MUTED,
                     font=("Segoe UI", 9)).pack(side="left")
            tk.Label(head, textvariable=txt, bg=self.PANEL, fg=self.FG,
                     font=("Segoe UI", 9, "bold")).pack(side="right")
            ttk.Progressbar(f, variable=var, maximum=100).pack(fill="x", pady=(4, 0))
        if not psutil:
            self.cpu_txt.set("pip install psutil")

        # таблица
        self.score_lbl = tk.Label(self, bg=self.BG, fg=self.MUTED, anchor="w",
                                  font=("Segoe UI", 10))
        self.score_lbl.pack(fill="x", padx=18, pady=(8, 2))
        cols = ("sel", "name", "cat", "cur", "rec", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for col, text, w, anchor in (("sel", "✓", 40, "center"), ("name", "Параметр", 300, "w"),
                                     ("cat", "Категория", 90, "w"), ("cur", "Сейчас", 150, "w"),
                                     ("rec", "Рекомендуется", 150, "w"), ("status", "Статус", 130, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor=anchor, stretch=(col == "name"))
        self.tree.tag_configure("ok", foreground=self.OK)
        self.tree.tag_configure("bad", foreground=self.WARN)
        self.tree.tag_configure("na", foreground=self.MUTED)
        self.tree.pack(fill="both", expand=True, padx=16, pady=4)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # описание
        self.desc = tk.Label(self, bg=self.PANEL, fg=self.FG, anchor="w", justify="left",
                             wraplength=900, font=("Segoe UI", 10), padx=12, pady=10,
                             text="Выберите параметр, чтобы увидеть описание.")
        self.desc.pack(fill="x", padx=16, pady=4)

        # кнопки
        bar = tk.Frame(self, bg=self.BG)
        bar.pack(fill="x", padx=16, pady=(4, 6))
        ttk.Button(bar, text="⟳ Сканировать", command=self.scan).pack(side="left")
        ttk.Button(bar, text="Точка восстановления",
                   command=self.restore_point).pack(side="left", padx=6)
        ttk.Button(bar, text="↺ Откатить всё", command=self.revert).pack(side="right")
        ttk.Button(bar, text="Применить выбранное", style="Accent.TButton",
                   command=self.apply).pack(side="right", padx=6)

        # лог
        self.log_box = tk.Text(self, height=5, bg=self.PANEL, fg=self.MUTED, relief="flat",
                               font=("Consolas", 9), state="disabled", padx=8, pady=6)
        self.log_box.pack(fill="x", padx=16, pady=(0, 14))

    # --- служебное
    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _tick(self):
        if psutil:
            self.cpu_var.set(psutil.cpu_percent())
            self.cpu_txt.set(f"{psutil.cpu_percent():.0f}%")
            vm = psutil.virtual_memory()
            self.ram_var.set(vm.percent)
            self.ram_txt.set(f"{vm.percent:.0f}%  ({vm.used / 2**30:.1f}/{vm.total / 2**30:.1f} ГБ)")
            try:
                d = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
                self.disk_var.set(d.percent)
                self.disk_txt.set(f"{d.percent:.0f}%  ({d.free / 2**30:.0f} ГБ свободно)")
            except Exception:
                pass
        self.after(2000, self._tick)

    def relaunch_admin(self):
        params = " ".join(f'"{a}"' for a in sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        self.destroy()

    # --- таблица
    def scan(self):
        self.tree.delete(*self.tree.get_children())
        good = 0
        for c in CHECKS:
            cur = c.read()
            ok = c.is_optimal()
            good += ok
            if ok:
                self.selected.discard(c.id)
            elif c.default_on and c.id not in self.selected and not getattr(self, "_scanned", False):
                self.selected.add(c.id)
            tag = "ok" if ok else "bad"
            status = "✔ Оптимально" if ok else ("⚠ Нужен админ" if c.admin and not is_admin()
                                                else "Можно улучшить")
            self.tree.insert("", "end", iid=c.id, tags=(tag,), values=(
                "☑" if c.id in self.selected else ("" if ok else "☐"),
                c.name + ("  ⟲" if c.reboot else ""), c.category,
                c.display(cur), c.display(c.recommended), status))
        self._scanned = True
        self.score_lbl.configure(text=f"Оптимально: {good} из {len(CHECKS)}   "
                                      f"(⟲ — потребуется перезагрузка)")
        self.log(f"Сканирование завершено: {good}/{len(CHECKS)} параметров в норме.")

    def _on_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        if self.tree.identify_column(e.x) != "#1":
            return
        iid = self.tree.identify_row(e.y)
        if not iid or self.by_id[iid].is_optimal():
            return
        self.selected.symmetric_difference_update({iid})
        vals = list(self.tree.item(iid, "values"))
        vals[0] = "☑" if iid in self.selected else "☐"
        self.tree.item(iid, values=vals)

    def _on_select(self, _):
        sel = self.tree.selection()
        if sel:
            c = self.by_id[sel[0]]
            extra = "\n⚠ Требует права администратора." if c.admin else ""
            self.desc.configure(text=c.description + extra)

    # --- действия
    def apply(self):
        todo = [self.by_id[i] for i in self.selected if not self.by_id[i].is_optimal()]
        if not todo:
            messagebox.showinfo(APP_NAME, "Нет выбранных параметров для применения.")
            return
        names = "\n".join("• " + c.name for c in todo)
        if not messagebox.askyesno(APP_NAME, f"Будут изменены параметры:\n\n{names}\n\n"
                                             "Старые значения сохранятся — их можно вернуть "
                                             "кнопкой «Откатить всё». Продолжить?"):
            return
        need_reboot = False
        for c in todo:
            if c.admin and not is_admin():
                self.log(f"✗ {c.name}: нужны права администратора")
                continue
            try:
                self.backup.remember(c)
                c.write(c.recommended)
                self.log(f"✔ {c.name}: применено")
                need_reboot |= c.reboot
            except Exception as ex:
                self.log(f"✗ {c.name}: {ex}")
        self.selected.clear()
        self.scan()
        if need_reboot:
            messagebox.showinfo(APP_NAME, "Часть изменений вступит в силу после перезагрузки.")

    def revert(self):
        if not self.backup.data:
            messagebox.showinfo(APP_NAME, "Нет сохранённых значений для отката.")
            return
        if not messagebox.askyesno(APP_NAME, "Вернуть все изменённые параметры к исходным значениям?"):
            return
        for cid, old in list(self.backup.data.items()):
            c = self.by_id.get(cid)
            if not c:
                continue
            try:
                c.write(old["value"] if old["existed"] else None)
                self.log(f"↺ {c.name}: восстановлено")
                del self.backup.data[cid]
            except Exception as ex:
                self.log(f"✗ {c.name}: {ex}")
        self.backup.save()
        self.scan()

    def restore_point(self):
        if not is_admin():
            messagebox.showwarning(APP_NAME, "Для создания точки восстановления нужны права администратора.")
            return
        self.log("Создание точки восстановления…")

        def work():
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 'Checkpoint-Computer -Description "PC Optimizer" -RestorePointType MODIFY_SETTINGS'],
                capture_output=True, text=True, creationflags=NO_WINDOW, errors="ignore")
            msg = ("✔ Точка восстановления создана" if r.returncode == 0
                   else "✗ Не удалось создать точку (включена ли защита системы? "
                        "Windows разрешает не чаще 1 раза в 24 ч)")
            self.after(0, lambda: self.log(msg))

        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    if winreg is None:
        print("Это приложение работает только в Windows.")
        sys.exit(1)
    App().mainloop()