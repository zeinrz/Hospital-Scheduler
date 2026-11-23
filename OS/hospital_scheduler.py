"""
Hospital CPU Scheduling Simulator  (ITEC 55)
Python 3.8+ | tkinter + Pillow (pip install pillow)        Run:  python hospital_scheduler.py
Starts fullscreen.  Esc / F11 = leave / enter fullscreen.

SCENARIO MAPPING (Story <-> OS)
  Patient ............ Process (PID)            Waiting Room ..... Ready queue
  Arrival time ....... patient registers (create_process, NEW -> READY)
  Clinic Room ........ CPU (one patient at a time)    Burst time ... minutes in the clinic
  Session Complete ... TERMINATED (terminate_process)  Policy ........ scheduling algorithm
  Doctor's Order ..... plain-language "Why?" of the latest scheduling step
  Doctor's office .... Kernel mode                    Patient area ... User mode
  Next / Previous .... Time Machine (every step is stored as a snapshot)

Layout: Sim (pure scheduling logic + kernel log)  ->  App (main screen)  ->  Popups.
The static artwork is baked once with Pillow at the screen's scale; moving parts are redrawn
on a tk.Canvas and the run loop uses after(), so the UI never freezes.
"""
import copy
import math
import os
import sys
import tkinter as tk
from tkinter import font as tkfont

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
except ImportError:
    sys.exit("Pillow is required.  Install it with:  pip install pillow")

HERE = os.path.dirname(os.path.abspath(__file__))
BG_FILE = os.path.join(HERE, "assets", "hospital_bg.jpg")
CLIP_FILE = os.path.join(HERE, "assets", "clipboard.png")
DOC_FILE = os.path.join(HERE, "assets", "doctor.png")

DW, DH = 1193, 687                   # design size (the Figma frame); everything is scaled from this
S = 3                                # supersampling for pop-up panels
TILT = 8.4                           # clipboard tilt (degrees, clockwise)
TEAL, TEAL2, GREY, POP_BG = "#17444a", "#1d5873", "#d9d9d9", "#f1f4f3"
COLORS = ["#c0392b", "#2980b9", "#27ae60", "#8e44ad", "#d35400", "#16a085", "#7f8c8d", "#b7950b"]
NAMES = ["Ana", "Ben", "Carla", "Dino", "Ella", "Fred", "Gina", "Hugo"]
MAX_PATIENTS = 8
POLICIES = [("First Come First Serve", "FCFS"), ("Shortest Job First", "SJF"),
            ("Non-Preemptive Priority", "PRIO"), ("Round Robin", "RR")]
# rows are (arrival, burst, priority); lower priority number = served first
PRESETS = {
    "Balanced":  [(0, 5, 3), (1, 3, 2), (2, 4, 1), (3, 2, 3), (4, 6, 2), (22, 3, 1)],
    "CPU Heavy": [(0, 9, 3), (1, 7, 2), (2, 10, 1), (3, 8, 3), (4, 6, 2), (5, 9, 1)],
    "I/O Heavy": [(0, 3, 3), (1, 1, 2), (2, 2, 1), (8, 1, 3), (9, 2, 2), (10, 1, 1)],
}

# ---- layout of the main screen, in design units (measured from the Figma frame)
PANEL = (59, 60, 801, 479)
DIVS = (240, 420, 603)
COL_X = (150, 330, 511, 702)
BTNS = {"next": (832, 71, 975, 107), "prev": (1006, 70, 1149, 107), "reset": (832, 123, 975, 160),
        "table": (1006, 123, 1149, 159), "kernel": (832, 176, 975, 213), "view": (1006, 176, 1149, 213)}
BUBBLE = (833, 234, 1161, 343)
GANTT, STRIP = (16, 499, 630, 561), (25, 525, 609, 546)
BAR_TOP = 582
CARDS = ((15, 593, 159, 668), (169, 593, 374, 668), (383, 593, 524, 668), (533, 593, 665, 668))
SLIDER = (182, 633, 357, 643)
PW, PH = 210, 54                                           # clipboard button size
_th = math.radians(TILT)
_O = (1039.5, 540.5)                                       # centre of the "Patients" button
_D = (-math.sin(_th) * 83.5, math.cos(_th) * 83.5)         # step between clipboard buttons
PILL_POLICY = (_O[0] - _D[0], _O[1] - _D[1])
PILL_PATIENTS = _O
PILL_PLAY = (_O[0] + _D[0], _O[1] + _D[1])


# =============================================================== scheduling logic
class Patient:
    def __init__(self, pid, name, at, bt, pr):
        self.pid, self.name, self.at, self.bt, self.pr = pid, name, at, bt, pr
        self.rem, self.end, self.state = bt, None, "NEW"     # NEW -> WAIT -> RUN -> DONE


class Sim:
    """One call to tick() advances the clock by one time unit.
    Every kernel-level event is written to self.log; self.order is the Doctor's Order text."""

    def __init__(self, rows, policy, quantum):
        self.ps = [Patient(i + 1, NAMES[i], *r) for i, r in enumerate(rows)]
        self.policy, self.quantum = policy, quantum
        self.t, self.queue, self.cur, self.qused, self.busy = 0, [], None, 0, 0
        self.gantt, self.log, self.decs = [], [], []         # gantt: (pid, decision id, context switch?)
        self.cs, self.last, self.dec, self.evs, self.kflag = 0, None, None, [], False
        self.admit()
        self.mode = "KERNEL" if self.kflag else "USER"
        self.finalize()

    # ---- bookkeeping
    def note(self, kind, rank, o, s, oo=None, so=None):
        """Kernel-mode event: goes to the kernel log AND the Doctor's Order."""
        self.kflag = True
        self.log.append((self.t, kind, o, s))
        self.evs.append((rank, oo or o, so or s))

    def say(self, rank, o, s):
        """User-mode remark: Doctor's Order only."""
        self.evs.append((rank, o, s))

    def finalize(self):
        ev = sorted(self.evs, key=lambda e: e[0])[:2]
        if not ev:
            self.order = ("Ready. Choose a policy, then press Play or Next.",
                          "The clinic is open. Choose a policy, then press Play or Next.")
        else:
            self.order = (" ".join(e[1] for e in ev), " ".join(e[2] for e in ev))

    def admit(self):                                         # arrival time reached -> waiting room
        for p in self.ps:
            if p.state == "NEW" and p.at <= self.t:
                p.state = "WAIT"
                self.queue.append(p)
                self.note("SYSCALL", 3,
                          f"create_process(): P{p.pid} PCB allocated, NEW->READY (priority {p.pr}, burst {p.bt}).",
                          f"{p.name} registers at reception and sits in the Waiting Room (NEW->READY).",
                          f"P{p.pid} arrives (NEW->READY).", f"{p.name} arrives and waits.")

    def done(self):
        return all(p.state == "DONE" for p in self.ps)

    def pick(self):
        r = [p for p in self.queue]
        qo, qs = "[" + ", ".join(f"P{p.pid}" for p in r) + "]", "[" + ", ".join(p.name for p in r) + "]"
        if self.policy == "SJF":
            p = min(r, key=lambda x: (x.rem, self.queue.index(x)))
            return p, f"SJF: P{p.pid} has the shortest burst ({p.rem}) in {qo}.", \
                f"{p.name} has the shortest visit ({p.rem} min) in {qs}."
        if self.policy == "PRIO":
            p = min(r, key=lambda x: (x.pr, self.queue.index(x)))
            return p, f"Priority: P{p.pid} has the best priority number ({p.pr}) in {qo}.", \
                f"{p.name} is the most urgent case (priority {p.pr}) in {qs}."
        p = r[0]
        if self.policy == "RR":
            return p, f"Round Robin: P{p.pid} is next in {qo}; slice = {self.quantum}.", \
                f"It is {p.name}'s turn in {qs}; they get {self.quantum} min."
        return p, f"FCFS: P{p.pid} is first in the ready queue {qo}.", f"{p.name} has waited longest in {qs}."

    def tick(self):
        if self.done():
            return False
        self.evs, self.kflag = [], False
        t, sw = self.t, False
        if self.cur is None and self.queue:                  # clinic free -> scheduler picks next patient
            p, o, s = self.pick()
            self.queue.remove(p)
            self.decs.append(dict(id=len(self.decs) + 1, t=t, pid=p.pid, os=o, story=s))
            self.dec = len(self.decs)
            sw = self.last is not None and self.last != p.pid
            if sw:
                lp = self.ps[self.last - 1]
                self.cs += 1
                self.note("SCHED", 1,
                          f"CONTEXT SWITCH P{lp.pid} -> P{p.pid}: save P{lp.pid} registers to its PCB, load P{p.pid}. {o}",
                          f"The doctor switches from {lp.name} to {p.name}. {s}",
                          f"Context switch to P{p.pid}. {o}", f"{p.name} is called in. {s}")
            else:
                same = " (same process as before: no context switch)" if self.last == p.pid else ""
                self.note("SCHED", 1, f"Dispatch P{p.pid} READY->RUNNING{same}. {o}",
                          f"{p.name} is called into the Clinic Room. {s}",
                          f"Dispatch P{p.pid}. {o}", f"{p.name} is called in. {s}")
            p.state, self.qused, self.cur, self.last = "RUN", 0, p, p.pid
        p = self.cur
        if p:                                                # one unit inside the clinic (user mode)
            p.rem -= 1
            self.busy += 1
            self.qused += 1
        self.gantt.append((p.pid, self.dec, sw) if p else (None, None, False))
        self.t += 1
        self.admit()                                         # arrivals enter before a preempted patient
        if p:
            if p.rem == 0:                                   # burst complete -> Session Complete
                p.state, p.end, self.cur = "DONE", self.t, None
                self.note("SYSCALL", 2, f"terminate_process(): P{p.pid} RUNNING->TERMINATED, resources freed.",
                          f"{p.name}'s session is complete and they leave the clinic.",
                          f"P{p.pid} terminates (RUNNING->TERMINATED).", f"{p.name}'s session is complete.")
            elif self.policy == "RR" and self.qused >= self.quantum:
                p.state, self.cur = "WAIT", None             # quantum expired -> back of the line
                self.queue.append(p)
                self.note("INTERRUPT", 2,
                          f"Timer interrupt: quantum {self.quantum} expired, P{p.pid} RUNNING->READY (back of queue).",
                          f"{p.name}'s {self.quantum} minutes are up; back to the Waiting Room.",
                          f"Quantum {self.quantum} expired: P{p.pid} back to READY.",
                          f"{p.name}'s {self.quantum} min are up; back to the Waiting Room.")
            else:
                self.say(4, f"P{p.pid} keeps the CPU ({p.rem} left).", f"{p.name} stays in the clinic ({p.rem} sec left).")
        else:
            self.say(4, "CPU idle: the ready queue is empty.", "The doctor waits: nobody is in the Waiting Room.")
        self.mode = "KERNEL" if self.kflag else "USER"
        self.finalize()
        return True

    def stats(self):
        f = [p for p in self.ps if p.state == "DONE"]
        att = sum(p.end - p.at for p in f) / len(f) if f else None
        wtt = sum(p.end - p.at - p.bt for p in f) / len(f) if f else None
        util = 100.0 * self.busy / self.t if self.t else 0.0
        return att, wtt, util


# ==================================================================== image baking
def _place(layer, img, x, y):
    """alpha_composite that tolerates images hanging outside the canvas."""
    lw, lh = layer.size
    l, t, r, b = max(0, -x), max(0, -y), min(img.width, lw - x), min(img.height, lh - y)
    if r > l and b > t:
        layer.alpha_composite(img.crop((l, t, r, b)), (x + l, y + t))


def pill_image(k, fill, outline):
    """A rounded button, tilted like the clipboard, smoothly anti-aliased, at screen scale k."""
    ss = max(1, round(3 / k))
    z = ss * k
    im = Image.new("RGBA", (round((PW + 8) * z), round((PH + 8) * z)), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((4 * z, 4 * z, (PW + 4) * z, (PH + 4) * z), radius=PH / 2 * z,
                                         fill=fill, outline=outline, width=max(1, round(z)))
    im = im.rotate(-TILT, expand=True, resample=Image.BICUBIC)
    return im.resize((max(1, round(im.width / ss)), max(1, round(im.height / ss))), Image.LANCZOS)


def panel_image(w, h, bg=POP_BG):
    """Rounded grey panel with a dark-teal border used by all pop-ups."""
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=30 * S, fill=(217, 217, 217, 255),
                                         outline=(23, 68, 74, 255), width=5 * S)
    im = im.resize((w, h), Image.LANCZOS)
    return Image.alpha_composite(Image.new("RGBA", (w, h), bg), im)


def build_scene(k):
    """Photo + translucent board + buttons + boxes + doctor + clipboard baked into one picture at scale k."""
    ss = max(1, round(3 / k))
    z = ss * k
    w, h = round(DW * k), round(DH * k)
    photo = Image.open(BG_FILE).convert("RGB")
    pk = 1.96 * k                                           # photo scale/offset fitted to the Figma frame
    photo = photo.resize((round(photo.width * pk), round(photo.height * pk)), Image.LANCZOS)
    photo = photo.filter(ImageFilter.UnsharpMask(1.2 * k, 60, 2))
    base = Image.new("RGBA", (w, h), (23, 68, 74, 255))
    base.paste(photo, (round(-6 * k), round(-56 * k)))

    layer = Image.new("RGBA", (round(DW * z), round(DH * z)), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    def rr(box, r, fill=None, outline=None, width=1):
        d.rounded_rectangle([v * z for v in box], radius=r * z, fill=fill, outline=outline,
                            width=max(1, round(width * z)))

    teal2, grey = (29, 88, 115, 255), (217, 217, 217, 255)
    rr(PANEL, 36, fill=(180, 185, 139, 153))                                         # translucent board
    for x in DIVS:
        d.line((x * z, (PANEL[1] + 6) * z, x * z, (PANEL[3] - 6) * z), fill=(29, 88, 115, 200), width=max(1, round(1.5 * z)))
    rr(PANEL, 36, outline=teal2, width=4)
    for b in BTNS.values():                                                          # 6 buttons
        rr(b, 10, fill=(226, 226, 226, 255), outline=(85, 85, 85, 255), width=1)
    rr(BUBBLE, 10, fill=grey, outline=(70, 70, 70, 255), width=1)                    # speech bubble + tail
    d.polygon([(880 * z, 342 * z), (897 * z, 342 * z), (888 * z, 356 * z)], fill=grey)
    d.line((888 * z, 356 * z, 880 * z, 343 * z), fill=(70, 70, 70, 255), width=max(1, round(z)))
    d.line((888 * z, 356 * z, 897 * z, 343 * z), fill=(70, 70, 70, 255), width=max(1, round(z)))
    rr(GANTT, 14, fill=grey, outline=teal2, width=3)                                 # Gantt box + white strip
    rr(STRIP, 3, fill=(255, 255, 255, 255))
    d.rectangle((0, BAR_TOP * z, DW * z, DH * z), fill=(23, 68, 74, 255))            # bottom bar
    for c in CARDS:
        rr(c, 10, fill=grey)
    rr(SLIDER, 5, fill=(188, 188, 188, 255))                                         # speed track
    layer = layer.resize((w, h), Image.LANCZOS)
    base = Image.alpha_composite(base, layer)

    top = Image.new("RGBA", (w, h), (0, 0, 0, 0))                                    # doctor, then clipboard
    doc = Image.open(DOC_FILE).convert("RGBA").crop((273, 36, 1468, 1414))
    ds = 0.255 * k
    doc = doc.resize((round(doc.width * ds), round(doc.height * ds)), Image.LANCZOS)
    _place(top, doc, round(630 * k), round(354 * k))
    cb = Image.open(CLIP_FILE).convert("RGBA").crop((511, 32, 1387, 1362))
    f = 0.33 * k
    cb = cb.resize((round(876 * f), round(1330 * f)), Image.LANCZOS)
    cb = cb.rotate(-TILT, expand=True, resample=Image.BICUBIC)
    _place(top, cb, round(1044 * k - cb.width / 2), round(524 * k - cb.height / 2))
    pill = pill_image(k, (214, 214, 214, 255), (150, 150, 150, 255))
    for c in (PILL_POLICY, PILL_PATIENTS):
        _place(top, pill, round(c[0] * k - pill.width / 2), round(c[1] * k - pill.height / 2))
    return Image.alpha_composite(base, top)


def rrect(cv, x0, y0, x1, y1, r, **kw):
    """Rounded rectangle on a tk.Canvas (smoothed polygon)."""
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


# ======================================================================== pop-ups
class Popup(tk.Toplevel):
    kind = ""

    def __init__(self, app, title, w, h):
        super().__init__(app.root)
        self.app, self.w, self.h = app, w, h
        self.title(title)
        self.resizable(False, False)
        self.configure(bg=POP_BG)
        self.transient(app.root)
        self.cv = tk.Canvas(self, width=w, height=h, bg=POP_BG, highlightthickness=0)
        self.cv.pack()
        self.panel = ImageTk.PhotoImage(panel_image(w - 30, h - 30))
        self.cv.create_image(15, 15, anchor="nw", image=self.panel)
        sw, sh = app.root.winfo_screenwidth(), app.root.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")
        self.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        self.app.pops.pop(self.kind, None)
        self.destroy()


class PolicyPopup(Popup):
    kind = "policy"

    def __init__(self, app):
        super().__init__(app, "Policy", 480, 340)
        self.qvar = tk.IntVar(value=app.quantum)
        self.draw()
        sp = tk.Spinbox(self.cv, from_=1, to=9, width=3, textvariable=self.qvar, command=self.set_q,
                        font=(app.F, 11), justify="center")
        sp.bind("<Return>", lambda e: self.set_q())
        sp.bind("<FocusOut>", lambda e: self.set_q())
        self.cv.create_window(400, 280, window=sp)

    def draw(self):
        cv, F = self.cv, self.app.F
        cv.delete("dyn")
        cv.create_text(45, 52, text="Policy", anchor="w", font=(F, 18, "bold"), fill="#111", tags="dyn")
        for i, (name, code) in enumerate(POLICIES):
            y, sel = 102 + i * 40, code == self.app.policy
            cv.create_rectangle(30, y - 17, 450, y + 17, fill=TEAL if sel else GREY, outline="", tags=("dyn", f"o{i}"))
            cv.create_text(45, y, text=name, anchor="w", font=(F, 13, "bold" if sel else "normal"),
                           fill="white" if sel else "#111", tags=("dyn", f"o{i}"))
            cv.tag_bind(f"o{i}", "<Button-1>", lambda e, c=code: self.choose(c))
        cv.create_text(45, 280, text="Time quantum (Round Robin)", anchor="w", font=(F, 11), fill="#111", tags="dyn")

    def relabel(self):
        self.draw()

    def choose(self, code):
        self.app.set_policy(code)
        self.draw()

    def set_q(self):
        try:
            q = max(1, min(9, int(self.qvar.get())))
        except (tk.TclError, ValueError):
            q = self.app.quantum
        self.qvar.set(q)
        self.app.set_quantum(q)


class PatientsPopup(Popup):
    kind = "patients"
    TABS = ["Balanced", "CPU Heavy", "I/O Heavy", "Custom"]
    XS, TX = [100, 255, 410, 565], [115, 265, 410, 560]

    def __init__(self, app):
        super().__init__(app, "Patients  (lower priority number = served first)", 680, 470)
        self.mode, self.ents, self.msg = app.preset, [], ""
        self.edit = [[str(v) for v in r] for r in app.custom]
        self.draw()

    def relabel(self):
        self.sync()
        self.draw()

    def draw(self):
        cv, F = self.cv, self.app.F
        cv.delete("dyn")
        for row in self.ents:
            for e in row:
                e.destroy()
        self.ents = []
        for x, t in zip(self.XS, ["PID", "Arrival Time", "Burst Time", "Priority"]):
            cv.create_text(x, 52, text=t, font=(F, 12, "bold"), fill="#111", tags="dyn")
        custom = self.mode == "Custom"
        for i, row in enumerate(self.edit if custom else PRESETS[self.mode]):
            y = 92 + i * 31
            cv.create_text(self.XS[0], y, text=self.app.nm_long(i), font=(F, 11), fill="#111", tags="dyn")
            if custom:
                es = []
                for j in range(3):
                    e = tk.Entry(cv, width=6, justify="center", font=(F, 11))
                    e.insert(0, str(row[j]))
                    cv.create_window(self.XS[j + 1], y, window=e, width=80, height=24, tags="dyn")
                    es.append(e)
                self.ents.append(es)
            else:
                for j in range(3):
                    cv.create_text(self.XS[j + 1], y, text=str(row[j]), font=(F, 11), fill="#111", tags="dyn")
        if custom:
            for x, txt, fn in ((90, "+ Add", self.add), (170, "- Remove", self.remove), (265, "Apply", self.apply)):
                b = tk.Button(cv, text=txt, command=fn, bg=TEAL, fg="white", relief="flat", font=(F, 10, "bold"),
                              padx=8, cursor="hand2")
                cv.create_window(x, 345, window=b, tags="dyn")
            cv.create_text(330, 345, text=self.msg, anchor="w", font=(F, 9), fill="#a02020", tags="dyn")
        cv.create_line(25, 372, 655, 372, fill=TEAL, tags="dyn")
        for i, name in enumerate(self.TABS):
            x, sel = self.TX[i], name == self.mode
            rrect(cv, x - 70, 389, x + 70, 435, 18, fill=TEAL if sel else GREY, outline="", tags=("dyn", f"t{i}"))
            cv.create_text(x, 412, text=name, font=(F, 13, "bold"), fill="white" if sel else "#111", tags=("dyn", f"t{i}"))
            cv.tag_bind(f"t{i}", "<Button-1>", lambda e, n=name: self.pick(n))

    def pick(self, name):
        self.sync()
        self.mode = name
        self.msg = "Edit the values, then press Apply." if name == "Custom" else ""
        if name != "Custom":
            self.app.set_rows(name, PRESETS[name])
        self.draw()

    def sync(self):                                   # copy typed text back into self.edit
        if self.ents:
            self.edit = [[e.get().strip() for e in row] for row in self.ents]

    def add(self):
        self.sync()
        if len(self.edit) < MAX_PATIENTS:
            last = self.edit[-1][0] if self.edit and self.edit[-1][0].isdigit() else "0"
            self.edit.append([str(int(last) + 1), "3", "2"])
        self.msg = ""
        self.draw()

    def remove(self):
        self.sync()
        if len(self.edit) > 1:
            self.edit.pop()
        self.msg = ""
        self.draw()

    def apply(self):
        self.sync()
        try:
            rows = [tuple(int(v) for v in r) for r in self.edit]
        except ValueError:
            self.msg = "Use whole numbers only."
            return self.draw()
        if any(a < 0 or b < 1 or p < 1 for a, b, p in rows):
            self.msg = "Arrival >= 0, burst >= 1, priority >= 1."
            return self.draw()
        self.msg = "Loaded! Press Play."
        self.app.custom = rows
        self.app.set_rows("Custom", rows)
        self.draw()


class TablePopup(Popup):
    kind = "table"
    XS = [80, 170, 250, 330, 410, 490]

    def __init__(self, app):
        super().__init__(app, "Results Table", 760, 450)
        self.cv.create_line(555, 30, 555, 420, fill=TEAL, width=2)
        self.refresh()

    def refresh(self):
        cv, F, sim = self.cv, self.app.F, self.app.cur()
        cv.delete("dyn")
        for x, t in zip(self.XS, ["PID", "AT", "BT", "ET", "TT", "WT"]):
            cv.create_text(x, 52, text=t, font=(F, 12, "bold"), fill="#111", tags="dyn")
        for i, p in enumerate(sim.ps):
            y = 92 + i * 30
            done = p.state == "DONE"
            vals = [self.app.nm_long(i), p.at, p.bt, p.end if done else "-",
                    p.end - p.at if done else "-", p.end - p.at - p.bt if done else "-"]
            for x, v in zip(self.XS, vals):
                cv.create_text(x, y, text=str(v), font=(F, 11), fill="#111", tags="dyn")
        att, wtt, util = sim.stats()
        fmt = lambda v: "-" if v is None else f"{v:.2f}"
        for y, lab, cap, val in ((52, "ATT", "Average Turnaround Time", fmt(att)),
                                 (160, "WTT", "Average Waiting Time", fmt(wtt)),
                                 (268, "CPU Util", "Busy time / elapsed time", f"{util:.1f}%")):
            cv.create_text(575, y, text=lab, anchor="w", font=(F, 12, "bold"), fill="#111", tags="dyn")
            cv.create_text(575, y + 20, text=cap, anchor="w", font=(F, 8), fill="#555", tags="dyn")
            cv.create_text(575, y + 46, text=val, anchor="w", font=(F, 16, "bold"), fill=TEAL, tags="dyn")
        cv.create_text(40, 410, anchor="w", font=(F, 8), fill="#555", tags="dyn",
                       text="AT arrival | BT burst | ET end time | TT turnaround = ET-AT | WT waiting = TT-BT")

    relabel = refresh


class KernelPopup(Popup):
    kind = "kernel"
    TAGS = {"SYSCALL": "#1f5fa8", "INTERRUPT": "#c0661a", "SCHED": "#7b2d8e"}
    STORY_TAG = {"SYSCALL": "STAFF CALL", "INTERRUPT": "BELL", "SCHED": "DOCTOR"}

    def __init__(self, app):
        super().__init__(app, "Kernel Log", 760, 450)
        box = tk.Frame(self.cv, bg=GREY)
        self.txt = tk.Text(box, font=("Courier", 10), wrap="word", bg="#f4f4f4", relief="flat", state="disabled")
        sb = tk.Scrollbar(box, command=self.txt.yview)
        self.txt.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        for tag, col in self.TAGS.items():
            self.txt.tag_configure(tag, foreground=col)
        self.cv.create_window(380, 262, window=box, width=700, height=280)
        self.refresh()

    def refresh(self):
        cv, F, s, os_ = self.cv, self.app.F, self.app.cur(), self.app.view == "OS"
        cv.delete("dyn")
        cv.create_text(45, 52, text="Kernel Log" if os_ else "Kernel Log  (Doctor's Office)", anchor="w",
                       font=(F, 18, "bold"), fill="#111", tags="dyn")
        kern = s.mode == "KERNEL"
        rrect(cv, 480, 32, 715, 72, 18, fill="#c0392b" if kern else "#27ae60", outline="", tags="dyn")
        cv.create_text(597, 52, fill="white", font=(F, 12, "bold"), tags="dyn",
                       text=("KERNEL MODE" if os_ else "DOCTOR'S OFFICE") if kern else ("USER MODE" if os_ else "PATIENT AREA"))
        cv.create_text(45, 90, anchor="w", font=(F, 10), fill="#333", tags="dyn",
                       text=f"Time {s.t}  |  context switches: {s.cs}  |  events logged: {len(s.log)}")
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        for t, kind, o, st in s.log:
            tag = kind if os_ else self.STORY_TAG[kind]
            self.txt.insert("end", f"[t={t:02d}] {tag:<10} {o if os_ else st}\n", kind)
        self.txt.see("end")
        self.txt.config(state="disabled")

    relabel = refresh


# ===================================================================== main screen
class App:
    def __init__(self, root):
        self.root = root
        root.title("Hospital CPU Scheduling Simulator")
        self.fullscreen = True
        root.attributes("-fullscreen", True)
        root.bind("<Escape>", lambda e: self.set_fullscreen(False))
        root.bind("<F11>", lambda e: self.set_fullscreen(not self.fullscreen))
        fams = set(tkfont.families(root))
        self.F = next((f for f in ("Segoe UI", "Helvetica Neue", "Ubuntu", "DejaVu Sans", "Arial") if f in fams), "TkDefaultFont")
        self.policy, self.quantum, self.preset, self.speed, self.view = "FCFS", 2, "Balanced", 0.5, "STORY"
        self.rows, self.custom = PRESETS["Balanced"], list(PRESETS["Balanced"])
        self.playing, self.job, self.rjob, self.drag, self.pops, self.pin = False, None, None, False, {}, None
        self.hist, self.idx = [], 0
        self.cv = tk.Canvas(root, bg=TEAL, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.size = (root.winfo_screenwidth(), root.winfo_screenheight())
        self.k, self.ox, self.oy = 1.0, 0.0, 0.0
        self.layout(*self.size)
        box = lambda b: (lambda x, y: b[0] <= x <= b[2] and b[1] <= y <= b[3])
        self.hot = [(box(BTNS["next"]), self.next), (box(BTNS["prev"]), self.prev), (box(BTNS["reset"]), self.reset),
                    (box(BTNS["table"]), lambda: self.open("table", TablePopup)),
                    (box(BTNS["kernel"]), lambda: self.open("kernel", KernelPopup)),
                    (box(BTNS["view"]), self.toggle_view),
                    (self.pill(PILL_POLICY), lambda: self.open("policy", PolicyPopup)),
                    (self.pill(PILL_PATIENTS), lambda: self.open("patients", PatientsPopup)),
                    (self.pill(PILL_PLAY), self.toggle_play)]
        self.cv.bind("<Button-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", False))
        self.cv.bind("<Motion>", self.on_move)
        self.cv.bind("<Configure>", self.on_cfg)
        self.reset()

    # ---- screen scaling: the design is scaled to fit the window and centred (letterboxed)
    @property
    def sim(self):
        return self.hist[self.idx]

    cur = lambda self: self.hist[self.idx]

    def layout(self, w, h):
        self.k = min(w / DW, h / DH)
        self.ox, self.oy = (w - DW * self.k) / 2, (h - DH * self.k) / 2
        self.scene = ImageTk.PhotoImage(build_scene(self.k))
        self.img_play = ImageTk.PhotoImage(pill_image(self.k, (27, 86, 110, 255), (15, 55, 72, 255)))
        self.img_stop = ImageTk.PhotoImage(pill_image(self.k, (192, 72, 38, 255), (130, 45, 22, 255)))
        self.cv.delete("scene")
        self.cv.create_image(self.ox, self.oy, anchor="nw", image=self.scene, tags="scene")
        self.cv.tag_lower("scene")
        if self.hist:
            self.refresh()

    def on_cfg(self, e):
        if e.width < 300 or e.height < 200 or (abs(e.width - self.size[0]) <= 2 and abs(e.height - self.size[1]) <= 2):
            return
        self.size = (e.width, e.height)
        if self.rjob:
            self.root.after_cancel(self.rjob)
        self.rjob = self.root.after(200, lambda: self.layout(*self.size))

    def set_fullscreen(self, on):
        self.fullscreen = on
        self.root.attributes("-fullscreen", on)
        if not on:
            self.root.geometry(f"{min(DW, self.root.winfo_screenwidth() - 80)}x{min(DH, self.root.winfo_screenheight() - 100)}+30+30")

    def X(self, x):
        return self.ox + x * self.k

    def Y(self, y):
        return self.oy + y * self.k

    def text(self, x, y, s, px=14, bold=True, fill="#111", anchor="center", angle=0, width=0):
        kw = {"angle": angle} if angle else {}
        if width:
            kw["width"] = width * self.k
        return self.cv.create_text(self.X(x), self.Y(y), text=s, anchor=anchor, fill=fill, tags="dyn",
                                   font=(self.F, -max(7, round(px * self.k)), "bold" if bold else "normal"), **kw)

    def nm(self, p):
        return f"P{p.pid}" if self.view == "OS" else p.name

    def nm_long(self, i):
        return f"P{i + 1}" if self.view == "OS" else f"P{i + 1}  {NAMES[i]}"

    # ---- hit testing (mouse position -> design units)
    def pill(self, c):
        co, si = math.cos(_th), math.sin(_th)
        def test(x, y):
            dx, dy = x - c[0], y - c[1]
            return abs(dx * co + dy * si) <= PW / 2 and abs(-dx * si + dy * co) <= PH / 2
        return test

    def pt(self, e):
        return (e.x - self.ox) / self.k, (e.y - self.oy) / self.k

    def on_slider(self, x, y):
        return SLIDER[0] - 8 <= x <= SLIDER[2] + 8 and SLIDER[1] - 10 <= y <= SLIDER[3] + 10

    def on_press(self, e):
        x, y = self.pt(e)
        if self.on_slider(x, y):
            self.drag = True
            return self.on_drag(e)
        if STRIP[0] <= x <= STRIP[2] and STRIP[1] <= y <= STRIP[3]:
            return self.gantt_click(x)
        for test, fn in self.hot:
            if test(x, y):
                return fn()

    def on_drag(self, e):
        if self.drag:
            self.speed = min(1.0, max(0.0, (self.pt(e)[0] - SLIDER[0]) / (SLIDER[2] - SLIDER[0])))
            self.refresh()

    def on_move(self, e):
        x, y = self.pt(e)
        over = self.on_slider(x, y) or any(t(x, y) for t, _ in self.hot)
        self.cv.config(cursor="hand2" if over else "")

    def gantt_click(self, x):
        """Why? Explainer: click a Gantt block to see why that patient was chosen."""
        s = self.cur()
        if not s.gantt:
            return
        cw = min(24.0, 580.0 / len(s.gantt))
        i = int((x - 27) // cw)
        if 0 <= i < len(s.gantt) and s.gantt[i][1]:
            d = s.decs[s.gantt[i][1] - 1]
            p = s.ps[d["pid"] - 1]
            self.pin = (f"Why P{p.pid} ran (decision at t={d['t']}): {d['os']}",
                        f"Why {p.name} was called in (t={d['t']}): {d['story']}")
            self.refresh()

    # ---- simulation control (after() keeps the UI responsive)
    def delay(self):
        return int(1200 - 1100 * self.speed)

    def reset(self):
        self.pause()
        self.hist, self.idx, self.pin = [Sim(self.rows, self.policy, self.quantum)], 0, None
        self.changed()

    def set_policy(self, code):
        self.policy = code
        self.reset()

    def set_quantum(self, q):
        self.quantum = q
        if self.policy == "RR":
            self.reset()

    def set_rows(self, preset, rows):
        self.preset, self.rows = preset, list(rows)
        self.reset()

    def pause(self):
        self.playing = False
        if self.job:
            self.root.after_cancel(self.job)
            self.job = None

    def forward(self):
        """Move to the next time unit: replay recorded history, or compute a new one."""
        if self.idx < len(self.hist) - 1:
            self.idx += 1
            return True
        n = copy.deepcopy(self.hist[-1])
        if not n.tick():
            return False
        self.hist.append(n)
        self.idx += 1
        return True

    def at_end(self):
        return self.idx == len(self.hist) - 1 and self.cur().done()

    def next(self):
        self.pause()
        self.pin = None
        self.forward()
        self.changed()

    def prev(self):                                   # Time Machine: one step back
        self.pause()
        self.pin = None
        if self.idx > 0:
            self.idx -= 1
        self.changed()

    def toggle_play(self):
        if self.playing:
            self.pause()
            return self.refresh()
        if self.at_end():
            self.reset()
        self.playing = True
        self.run_tick()

    def run_tick(self):
        self.job = None
        if not self.playing:
            return
        self.pin = None
        ok = self.forward()
        if not ok or self.at_end():
            self.playing = False
        else:
            self.job = self.root.after(self.delay(), self.run_tick)
        self.changed()

    def toggle_view(self):                            # Dual View: OS wording <-> hospital story
        self.view = "OS" if self.view == "STORY" else "STORY"
        self.refresh()
        for w in list(self.pops.values()):
            w.relabel()

    def open(self, kind, cls):
        w = self.pops.get(kind)
        if w:
            return w.lift()
        self.pops[kind] = cls(self)

    def changed(self):
        self.refresh()
        for kind in ("table", "kernel"):
            if kind in self.pops:
                self.pops[kind].refresh()

    # ---- drawing of everything that moves
    def refresh(self):
        cv, s = self.cv, self.cur()
        os_ = self.view == "OS"
        T = lambda o, st: o if os_ else st
        cv.delete("dyn")
        for x, t in zip((156, 330, 510, 702), T(("New", "Ready Queue", "CPU", "Terminated"),
                                                ("Patients", "Waiting Room", "Clinic Room", "Session Complete"))):
            self.text(x, 102, t, 16)
        for key, label in (("next", "Next"), ("prev", "Previous"), ("reset", "Reset"), ("table", "Table"),
                           ("kernel", "Kernel"), ("view", "Story View" if os_ else "OS View")):
            b = BTNS[key]
            self.text((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, label, 17)
        # board: one chip per patient in the column of its state
        cols = [sorted([p for p in s.ps if p.state == "NEW"], key=lambda p: (p.at, p.pid)),
                list(s.queue), [s.cur] if s.cur else [], [p for p in s.ps if p.state == "DONE"]]
        for ci, lst in enumerate(cols):
            for j, p in enumerate(lst):
                y = 135 + j * 38
                txt = (T(f"P{p.pid}  AT {p.at}", f"{p.name}  AT {p.at}"),
                       T(f"P{p.pid}  rem {p.rem}", f"{p.name}  {p.rem} sec left"),
                       T(f"P{p.pid}  rem {p.rem}", f"{p.name}  {p.rem} sec left"),
                       T(f"P{p.pid}  end {p.end}", f"{p.name}  done @{p.end}s"))[ci]
                rrect(cv, self.X(COL_X[ci] - 75), self.Y(y), self.X(COL_X[ci] + 75), self.Y(y + 30), 9 * self.k,
                      fill=COLORS[(p.pid - 1) % 8], outline="white" if ci == 2 else "", width=3, tags="dyn")
                self.text(COL_X[ci], y + 15, txt, 13, fill="white")
        self.draw_gantt(s, T)
        # bottom boxes
        for c, lab in zip(CARDS, ("Clock", "Speed", "CPU State", "Run State")):
            self.text(c[0] + 14, 612, lab, 12, fill=TEAL2, anchor="w")
        state = "Finished" if self.at_end() else ("Running" if self.playing else "Paused")
        cpu = T(f"P{s.cur.pid} RUNNING", f"{s.cur.name} (P{s.cur.pid})") if s.cur else T("IDLE", "Idle")
        for c, v in zip((CARDS[0], CARDS[2], CARDS[3]), (T(f"T = {s.t}", f"Time = {s.t}s"), cpu, state)):
            self.text((c[0] + c[2]) / 2, 641, v, 16)
        kx = SLIDER[0] + self.speed * (SLIDER[2] - SLIDER[0])
        cv.create_line(self.X(SLIDER[0] + 5), self.Y(638), self.X(max(kx, SLIDER[0] + 5)), self.Y(638),
                       width=10 * self.k, capstyle="round", fill=TEAL2, tags="dyn")
        cv.create_oval(self.X(kx - 8), self.Y(630), self.X(kx + 8), self.Y(646), fill="white", outline=TEAL2,
                       width=2 * self.k, tags="dyn")
        self.text(357, 612, f"{1000 / self.delay():.1f}x", 12, fill=TEAL2, anchor="e")
        # Doctor's Order (speech bubble): the action of the CPU and why
        self.text(843, 250, T("Scheduler Decision", "Doctor's Order"), 15, anchor="w")
        order = T(*(self.pin or s.order))
        self.text(843, 266, order if len(order) < 215 else order[:212] + "...", 12, bold=False, anchor="nw", width=308)
        # clipboard buttons
        self.text(PILL_POLICY[0], PILL_POLICY[1], "Policy", 17, angle=-TILT)
        self.text(PILL_PATIENTS[0], PILL_PATIENTS[1], T("Processes", "Patients"), 17, angle=-TILT)
        cv.create_image(self.X(PILL_PLAY[0]), self.Y(PILL_PLAY[1]), image=self.img_stop if self.playing else self.img_play,
                        tags="dyn")
        self.text(PILL_PLAY[0], PILL_PLAY[1], "Stop" if self.playing else "Play", 17, fill="white", angle=-TILT)

    def draw_gantt(self, s, T):
        self.text(25, 513, "Gantt Chart", 12, fill=TEAL2, anchor="w")
        n = len(s.gantt)
        if not n:
            return
        x0, cw = 27.0, min(24.0, 580.0 / n)               # shrink cells so the whole chart always fits
        step = max(1, math.ceil(18 / cw))
        for i, (pid, dec, cs) in enumerate(s.gantt):
            x = x0 + i * cw
            self.cv.create_rectangle(self.X(x), self.Y(527), self.X(x + cw), self.Y(544), tags="dyn", outline="white",
                                     fill=COLORS[(pid - 1) % 8] if pid else "#b8b8b8")
            if pid:
                full = T(f"P{pid}", s.ps[pid - 1].name[:3])
                lab = full if cw >= (16 if self.view == "OS" else 22) else (str(pid) if cw >= 9 else "")
                if lab:
                    self.text(x + cw / 2, 535.5, lab, 10, fill="white")
            if cs:                                       # red bar = context switch
                self.cv.create_line(self.X(x), self.Y(523), self.X(x), self.Y(548), fill="#d02020",
                                    width=max(2, 2 * self.k), tags="dyn")
            if i % step == 0:
                self.text(x, 553, str(i), 9, bold=False, fill=TEAL)
        if n % step == 0:
            self.text(x0 + n * cw, 553, str(n), 9, bold=False, fill=TEAL)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()