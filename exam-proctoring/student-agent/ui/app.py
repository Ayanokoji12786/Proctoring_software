"""Minimal Tkinter UI for the Student Agent.

Deliberately simple (spec: "do not make the agent visually complicated"):
one window showing identity, connection/camera/monitoring status, the
current detected state, an event counter, and an End Exam button. A consent
dialog is shown before any monitoring starts (privacy: explicit consent,
visible categories of data collected) and a persistent banner reminds the
student that monitoring is active while the exam is running.
"""
from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

CONSENT_CATEGORIES = [
    ("Active window / application name", "Detects switching away from the exam application."),
    ("Display configuration", "Detects mirrored displays or additional connected screens on this computer only."),
    ("Webcam video — processed locally only", "Checks you are present and facing the screen. Video is analyzed on this computer and is never streamed to the server."),
    ("Occasional still snapshot images", "Captured only for high-severity events, stored securely, auto-deleted after the retention period."),
    ("Heartbeat / connection signal", "Lets the proctor know your agent is still connected."),
]

_STATUS_COLORS = {
    "connected": "#1a7f37",
    "online": "#1a7f37",
    "active": "#1a7f37",
    "connecting": "#9a6700",
    "disconnected": "#cf222e",
    "auth_failed": "#cf222e",
    "unavailable": "#cf222e",
    "stopped": "#57606a",
    "unknown": "#57606a",
}


class ConsentDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, on_result: Callable[[bool], None]) -> None:
        super().__init__(parent)
        self.title("Exam Monitoring — Consent Required")
        self.geometry("460x420")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: self._respond(False))
        self._on_result = on_result

        tk.Label(
            self, text="This exam session will be monitored", font=("Helvetica", 13, "bold"), wraplength=420, justify="left"
        ).pack(padx=16, pady=(16, 4), anchor="w")
        tk.Label(
            self,
            text="Monitoring only starts after you consent below. The following categories "
            "of information will be collected during this exam session:",
            wraplength=420,
            justify="left",
        ).pack(padx=16, pady=(0, 10), anchor="w")

        list_frame = tk.Frame(self)
        list_frame.pack(padx=16, fill="both", expand=True)
        for title, desc in CONSENT_CATEGORIES:
            row = tk.Frame(list_frame)
            row.pack(fill="x", pady=4, anchor="w")
            tk.Label(row, text=f"• {title}", font=("Helvetica", 10, "bold"), anchor="w", justify="left", wraplength=420).pack(anchor="w")
            tk.Label(row, text=desc, font=("Helvetica", 9), fg="#57606a", anchor="w", justify="left", wraplength=420).pack(anchor="w")

        tk.Label(
            self,
            text="Monitoring stops automatically when you end the exam. No monitoring continues afterward.",
            font=("Helvetica", 9, "italic"),
            fg="#57606a",
            wraplength=420,
            justify="left",
        ).pack(padx=16, pady=(8, 8), anchor="w")

        button_row = tk.Frame(self)
        button_row.pack(pady=12)
        tk.Button(button_row, text="Decline (exit)", width=16, command=lambda: self._respond(False)).pack(side="left", padx=6)
        tk.Button(
            button_row, text="I Consent — Begin", width=18, bg="#1a7f37", fg="white",
            command=lambda: self._respond(True),
        ).pack(side="left", padx=6)

        self.grab_set()

    def _respond(self, consented: bool) -> None:
        self.grab_release()
        self.destroy()
        self._on_result(consented)


class AgentWindow(tk.Tk):
    def __init__(self, session_id: str, student_id: str, on_end_exam: Callable[[], None]) -> None:
        super().__init__()
        self.title("Exam Proctoring Agent")
        self.geometry("380x460")
        self.resizable(False, False)
        self._on_end_exam = on_end_exam
        self._queue: "queue.Queue[dict]" = queue.Queue()

        self.banner = tk.Label(
            self, text="● MONITORING ACTIVE", bg="#cf222e", fg="white",
            font=("Helvetica", 11, "bold"), pady=6,
        )
        self.banner.pack(fill="x")

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)

        self._vars: dict[str, tk.StringVar] = {}
        self._dots: dict[str, tk.Canvas] = {}

        self._add_static_row(form, "Exam Session", session_id)
        self._add_static_row(form, "Student ID", student_id)
        self._add_status_row(form, "connection", "Connection")
        self._add_status_row(form, "camera", "Camera")
        self._add_status_row(form, "monitoring", "Monitoring")
        self._add_static_row(form, "current_state", "Current State", initial="Initializing…")
        self._add_static_row(form, "event_count", "Events Generated", initial="0")

        ttk.Separator(form).pack(fill="x", pady=10)

        tk.Label(
            form,
            text="Signals shown here are flagged for proctor review. They are not\n"
            "automatic accusations of misconduct.",
            font=("Helvetica", 8, "italic"),
            fg="#57606a",
            justify="left",
        ).pack(anchor="w")

        tk.Button(
            self, text="End Exam", bg="#cf222e", fg="white", font=("Helvetica", 11, "bold"),
            command=self._handle_end_exam,
        ).pack(fill="x", padx=16, pady=16)

        self.after(150, self._poll_queue)

    def _add_static_row(self, parent: tk.Frame, key: str, label: str, initial: str = "—") -> None:
        row = tk.Frame(parent)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, width=16, anchor="w", font=("Helvetica", 10, "bold")).pack(side="left")
        var = tk.StringVar(value=initial)
        tk.Label(row, textvariable=var, anchor="w", wraplength=200, justify="left").pack(side="left", fill="x")
        self._vars[key] = var

    def _add_status_row(self, parent: tk.Frame, key: str, label: str) -> None:
        row = tk.Frame(parent)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, width=16, anchor="w", font=("Helvetica", 10, "bold")).pack(side="left")
        dot = tk.Canvas(row, width=12, height=12, highlightthickness=0)
        dot.pack(side="left", padx=(0, 6))
        dot.create_oval(1, 1, 11, 11, fill="#57606a", outline="", tags="dot")
        self._dots[key] = dot
        var = tk.StringVar(value="Unknown")
        tk.Label(row, textvariable=var, anchor="w").pack(side="left")
        self._vars[key] = var

    def push_update(self, **kwargs) -> None:
        """Thread-safe: call from any thread (asyncio loop, camera thread) to update the UI."""
        self._queue.put(kwargs)

    def _poll_queue(self) -> None:
        try:
            while True:
                update = self._queue.get_nowait()
                self._apply_update(update)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _apply_update(self, update: dict) -> None:
        for key in ("connection", "camera", "monitoring"):
            if key in update:
                status = str(update[key])
                self._vars[key].set(status.replace("_", " ").title())
                color = _STATUS_COLORS.get(status.lower(), "#57606a")
                self._dots[key].itemconfig("dot", fill=color)
        if "current_state" in update:
            self._vars["current_state"].set(str(update["current_state"]).replace("_", " ").title())
        if "event_count" in update:
            self._vars["event_count"].set(str(update["event_count"]))

    def _handle_end_exam(self) -> None:
        self._on_end_exam()


def show_consent_dialog(root: tk.Tk) -> bool:
    """Blocks until the dialog is closed (via Tk's wait_window), returns True if consented."""
    result: dict[str, bool] = {}

    def _capture(consented: bool) -> None:
        result["consented"] = consented

    dialog = ConsentDialog(root, _capture)
    root.wait_window(dialog)
    return result.get("consented", False)
