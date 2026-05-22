import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import subprocess
import threading
import json
from PIL import Image, ImageTk
import time
import os

settings = {}

def load_settings():
    if os.path.exists("settings.json"):
        with open("settings.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_settings():
    with open("settings.json", "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

def select_gradlew():
    path = filedialog.askopenfilename(title="Виберіть gradlew.bat", filetypes=[("BAT files", "*.bat")])
    if path:
        gradlew_entry.delete(0, tk.END)
        gradlew_entry.insert(0, path)
        settings["gradlew_path"] = path
        save_settings()

def connect_signal():
    qr_window = tk.Toplevel(root)
    qr_window.title("Зчитування QR-коду")
    qr_window.geometry("300x300")

    canvas = tk.Canvas(qr_window, width=300, height=300)
    canvas.pack()

    frames = [ImageTk.PhotoImage(Image.open("ouroboros.png").rotate(i)) for i in range(0, 360, 30)]

    anim_id = {"id": None}

    def animate(index=0):
        canvas.delete("all")
        canvas.create_image(150, 150, image=frames[index])
        anim_id["id"] = qr_window.after(100, animate, (index + 1) % len(frames))

    animate()

    def worker():
        try:
            gradlew_path = gradlew_entry.get()
            result = subprocess.run(
                [gradlew_path, "run", "--args=link -n ScreenshotBot"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60
            )
            output = result.stdout + result.stderr
            for line in output.splitlines():
                if "sgnl://" in line:
                    link = line.strip()
                    break
            else:
                raise ValueError("Не знайдено QR-посилання")

            qr_window.after_cancel(anim_id["id"])
            canvas.delete("all")

            # Виведення QR-коду
            import qrcode
            from io import BytesIO

            qr_img = qrcode.make(link)
            img_bytes = BytesIO()
            qr_img.save(img_bytes, format="PNG")
            img_bytes.seek(0)

            qr = Image.open(img_bytes)
            qr = qr.resize((250, 250), Image.ANTIALIAS)
            qr_imgtk = ImageTk.PhotoImage(qr)
            canvas.create_image(150, 150, image=qr_imgtk)
            canvas.image = qr_imgtk

        except subprocess.TimeoutExpired:
            qr_window.after_cancel(anim_id["id"])
            messagebox.showerror("Помилка", "Запит перевищив час очікування")
        except Exception as e:
            qr_window.after_cancel(anim_id["id"])
            messagebox.showerror("Помилка", str(e))

    threading.Thread(target=worker).start()

def load_groups():
    try:
        signal_cli = filedialog.askopenfilename(title="Виберіть signal-cli.bat", filetypes=[("BAT files", "*.bat")])
        phone = phone_entry.get()
        if not signal_cli or not phone:
            messagebox.showerror("Помилка", "Заповніть номер телефону та оберіть signal-cli")
            return

        result = subprocess.run(
            [signal_cli, "-u", phone, "listGroups", "--output=json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:
            messagebox.showerror("Помилка", f"Помилка signal-cli:\n{result.stderr}")
            return

        if not result.stdout:
            messagebox.showerror("Помилка", "signal-cli не повернув результат")
            return

        groups = json.loads(result.stdout.strip())
        group_combo["values"] = [f"{g['name']} ({g['id']})" for g in groups]
        group_combo.group_data = {f"{g['name']} ({g['id']})": g['id'] for g in groups}

    except json.JSONDecodeError as e:
        messagebox.showerror("JSON помилка", f"Неможливо розпізнати JSON:\n{e}")
    except Exception as e:
        messagebox.showerror("Помилка", str(e))

def save_group_selection():
    selected = group_combo.get()
    group_id = group_combo.group_data.get(selected)
    if group_id:
        settings["group_id"] = group_id
        settings["phone"] = phone_entry.get()
        save_settings()
        messagebox.showinfo("OK", f"Збережено групу:\n{selected}")
    else:
        messagebox.showerror("Помилка", "Оберіть групу")

# === Головне вікно ===
root = tk.Tk()
root.title("Налаштування Signal")
root.geometry("500x300")

settings = load_settings()

tk.Label(root, text="gradlew.bat:").pack()
gradlew_entry = tk.Entry(root, width=60)
gradlew_entry.pack()
gradlew_entry.insert(0, settings.get("gradlew_path", ""))
tk.Button(root, text="Огляд", command=select_gradlew).pack(pady=5)

tk.Button(root, text="Підключити акаунт Signal", command=connect_signal).pack(pady=10)

tk.Label(root, text="Номер телефону (у форматі +380...):").pack()
phone_entry = tk.Entry(root, width=30)
phone_entry.pack()
phone_entry.insert(0, settings.get("phone", ""))

tk.Button(root, text="Зчитати групи", command=load_groups).pack(pady=5)

tk.Label(root, text="Оберіть групу:").pack()
group_combo = ttk.Combobox(root, width=50)
group_combo.pack()
group_combo.group_data = {}

tk.Button(root, text="Зберегти вибір групи", command=save_group_selection).pack(pady=10)

root.mainloop()
